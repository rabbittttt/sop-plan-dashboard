import json
import http.client
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from sop_store import (ConflictError, SOPStore, TZ, ValidationError, WEIGHTS,
                       cutoff_for, horizon, month_add, stamp, sunday_before, week_zero_for_month)
from server import make_handler


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sales_path = self.root / "sales.json"
        self.sales_path.write_text(json.dumps({
            "meta": {"data_through": "2026-08-18"},
            "products": [{"id": "a", "brand": "A", "model": "M", "version": "2026"},
                         {"id": "b", "brand": "A", "model": "M", "version": "2025"}],
            "sales": [{"product_id": "a", "month": "2026-08", "metric": "locked_orders", "value": 123}],
            "sources": [],
        }), encoding="utf-8")
        self.current = datetime(2026, 6, 28, 12, tzinfo=TZ)
        self.store = SOPStore(self.root / "test.sqlite3", self.sales_path, lambda: self.current)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, pid="a", month="2026-10", sop=100, forecast=None, revision=0):
        return {"product_id": pid, "month": month, "sop": sop, "forecast": forecast, "revision": revision}

    def save(self, *changes):
        return self.store.save_plans({"changes": list(changes), "owner": "Test Owner", "reason": "test"})

    def ship(self, pid="a", value=100, month="2026-10", status="settled", revision=0, source="SP", batch=""):
        return self.store.save_shipments({"owner": "Test Owner", "changes": [{"product_id": pid, "month": month,
            "quantity": value, "status": status, "source": source, "batch": batch, "revision": revision}]})

    def finish_month(self):
        self.current = datetime(2026, 11, 1, 0, 1, tzinfo=TZ)

    def test_horizon_and_calendar_boundaries(self):
        self.assertEqual(horizon("2026-09"), "2027-12")
        self.assertEqual(horizon("2027-06"), "2027-12")
        self.assertEqual(horizon("2027-07"), "2028-01")
        self.assertEqual(horizon("2027-08"), "2028-02")
        self.assertEqual(horizon("2027-08", False), "2027-12")
        self.assertEqual(horizon("2027-09"), "2028-12")
        self.assertEqual(month_add("2026-12", 1), "2027-01")
        self.assertEqual(sunday_before("2026-11").isoformat(), "2026-10-25")  # First day is Sunday.
        self.assertEqual(sunday_before("2026-06").isoformat(), "2026-05-31")
        self.assertEqual(cutoff_for(sunday_before("2026-06")).isoformat(), "2026-06-01T00:00:00+08:00")
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1)

    def test_zero_week_strict_majority_all_seven_weekdays(self):
        cases = [
            ("2027-02", "2027-01-31"),  # Mon: 7 days -> prior week.
            ("2026-09", "2026-08-30"),  # Tue: 6 days -> prior week.
            ("2026-07", "2026-06-28"),  # Wed: 5 days -> prior week.
            ("2026-10", "2026-10-04"),  # Thu: exactly 4 -> same week.
            ("2027-01", "2027-01-03"),  # Fri: 3 days -> same week.
            ("2026-08", "2026-08-02"),  # Sat: 2 days -> same week.
            ("2026-11", "2026-11-01"),  # Sun: 1 day -> same week.
        ]
        for month, expected in cases:
            with self.subTest(month=month):
                self.assertEqual(week_zero_for_month(month).isoformat(), expected)
        self.assertEqual((week_zero_for_month("2027-01") - timedelta(weeks=13)).isoformat(), "2026-10-04")
        self.assertEqual(self.store.settings()["zero_week_anchor"], "month_week_majority")

    def test_null_zero_validation_atomicity_and_persistence(self):
        self.save(self.plan(sop=0, forecast=None))
        item = self.store.state()["plans"][0]
        self.assertEqual(item["sop"], 0)
        self.assertIsNone(item["forecast"])
        for invalid in [-1, 1.5, True, "100"]:
            with self.assertRaises(ValidationError):
                self.save(self.plan(pid="a", revision=1), self.plan(pid="b", sop=invalid))
        self.assertEqual(len(self.store.state()["plans"]), 1)
        self.store.close()
        self.store = SOPStore(self.root / "test.sqlite3", self.sales_path, lambda: self.current)
        self.assertEqual(self.store.state()["plans"][0]["revision"], 1)
        self.assertEqual(len(self.store.history()["items"]), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE events SET owner='changed'")

    def test_concurrency_and_bulk_conflict_rolls_back(self):
        self.save(self.plan())
        outcomes = []
        barrier = threading.Barrier(2)

        def write(value):
            barrier.wait()
            try:
                self.save(self.plan(sop=value, revision=1))
                outcomes.append("saved")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=write, args=(value,)) for value in (200, 300)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        with self.assertRaises(ConflictError):
            self.save(self.plan(pid="b"), self.plan(pid="a", revision=1))
        self.assertEqual(len(self.store.state()["plans"]), 1)

    def test_cutoff_excludes_equal_and_later_edits_and_backfills_truthfully(self):
        self.save(self.plan(sop=100))
        self.current = datetime(2026, 6, 29, 0, 0, tzinfo=TZ)
        self.save(self.plan(sop=200, revision=1))
        snapshot = self.store.snapshots()["items"][0]
        self.assertEqual(snapshot["kind"], "sunday")
        self.assertEqual(snapshot["business_date"], "2026-06-28")
        self.assertEqual(self.store.get_snapshot(snapshot["id"])["plans"][0]["sop"], 100)
        # Simulate downtime; recreation timestamp must not pretend to be cutoff time.
        self.current = datetime(2026, 7, 2, 10, 0, tzinfo=TZ)
        self.store.ensure_daily_snapshots()
        snapshots = self.store.snapshots()["items"]
        self.assertEqual(len(snapshots), 4)
        june29 = next(s for s in snapshots if s["business_date"] == "2026-06-29")
        self.assertEqual(june29["created_at"], stamp(self.current))
        self.assertEqual(self.store.get_snapshot(june29["id"])["plans"][0]["sop"], 200)
        self.assertFalse(any(s["business_date"] < "2026-06-28" for s in snapshots))

    def test_immutable_manual_snapshots_and_comparison(self):
        self.save(self.plan(sop=100))
        first = self.store.manual_snapshot("before")
        self.current += timedelta(seconds=1)
        self.save(self.plan(sop=150, revision=1))
        second = self.store.manual_snapshot("after")
        difference = self.store.compare(first["snapshot"]["id"], second["snapshot"]["id"])
        self.assertEqual(difference["items"][0]["sop_before"], 100)
        self.assertEqual(difference["items"][0]["sop_after"], 150)
        self.assertEqual(self.store.get_snapshot(first["snapshot"]["id"])["plans"][0]["sop"], 100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM snapshots")

    def test_matching_aggregates_before_ratio_and_requires_all_weeks(self):
        self.save(self.plan(sop=200), self.plan(pid="b", sop=20))
        self.finish_month()
        self.ship(value=100)
        self.ship(pid="b", value=60)
        result = self.store.matching("2026-10", brand="A")
        self.assertEqual(result["zero_week"], "2026-10-04")
        self.assertEqual(result["rows"][0]["business_date"], "2026-07-05")
        self.assertEqual(result["complete_weeks"], 14)
        self.assertEqual(result["actual"], 160)
        self.assertAlmostEqual(result["score"], 160 / 220 * 100)
        self.assertEqual(result["status"], "official")
        self.assertEqual([row["week"] for row in result["rows"]], list(range(13, -1, -1)))

    def test_missing_pending_unfinished_and_zero_are_distinct(self):
        self.save(self.plan(sop=0))
        self.finish_month()
        no_actual = self.store.matching("2026-10", product_id="a")
        self.assertIsNone(no_actual["actual"])
        self.assertIsNone(no_actual["score"])
        self.ship(value=0)
        both_zero = self.store.matching("2026-10", product_id="a")
        self.assertIsNone(both_zero["score"])
        self.assertTrue(all(row["status"] == "undefined_zero" for row in both_zero["rows"]))
        self.ship(value=50, revision=1, status="pending")
        self.assertEqual(self.store.matching("2026-10", product_id="a")["actual_status"], "pending")
        self.ship(value=50, revision=2, status="corrected", source="IoC", batch="latest")
        zero_plan = self.store.matching("2026-10", product_id="a")
        self.assertEqual(zero_plan["score"], 0)
        self.assertEqual(len(self.store.state()["shipments"]), 1)
        self.assertEqual(self.store.state()["shipments"][0]["source"], "IoC")
        self.assertEqual(len([e for e in self.store.history()["items"] if e["entity"] == "shipment"]), 3)
        aggregate_missing = self.store.matching("2026-10", brand="A")
        self.assertIsNone(aggregate_missing["score"])
        self.assertIsNone(aggregate_missing["actual"])
        self.ship(pid="b", value=1)
        missing_plan = self.store.matching("2026-10", brand="A")
        self.assertEqual(missing_plan["rows"][0]["status"], "missing_plan")
        self.assertIsNone(missing_plan["score"])

    def test_recent_months_are_calendar_months_and_refresh_preserves_plan(self):
        self.save(self.plan())
        self.current = datetime(2026, 10, 22, tzinfo=TZ)
        data = json.loads(self.sales_path.read_text(encoding="utf-8"))
        data["sales"][0]["value"] = 999
        self.sales_path.write_text(json.dumps(data), encoding="utf-8")
        self.store.record_sales()
        result = self.store.state()
        self.assertEqual(result["history_months"], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(result["plans"][0]["sop"], 100)
        self.assertEqual(result["sales"][0]["value"], 999)

    def test_unfinished_month_and_missing_week_never_publish_score(self):
        self.save(self.plan(sop=100))
        self.current = datetime(2026, 10, 15, tzinfo=TZ)
        self.ship(value=100)
        unfinished = self.store.matching("2026-10", product_id="a")
        self.assertEqual(unfinished["actual_status"], "in_progress")
        self.assertEqual(unfinished["complete_weeks"], 14)
        self.assertEqual(unfinished["scored_weeks"], 0)
        self.assertIsNone(unfinished["score"])
        self.finish_month()
        # Move the anchor back one week: W13 predates the initial service period.
        missing = self.store.matching("2026-10", product_id="a", zero_week_date="2026-09-20")
        self.assertEqual(missing["complete_weeks"], 13)
        self.assertEqual(missing["rows"][0]["status"], "missing_snapshot")
        self.assertIsNone(missing["score"])

    def test_snapshots_freeze_settings_and_use_business_month(self):
        self.current = datetime(2026, 7, 1, 1, tzinfo=TZ)
        self.store.update_settings({"bridge_m6": False})
        june30 = next(s for s in self.store.snapshots()["items"] if s["business_date"] == "2026-06-30")
        saved = self.store.get_snapshot(june30["id"])
        self.assertTrue(saved["settings"]["bridge_m6"])
        self.assertEqual(saved["meta"]["months"][0], "2026-06")
        self.current = datetime(2026, 7, 3, 12, tzinfo=TZ)
        self.store.ensure_daily_snapshots()
        july2 = next(s for s in self.store.snapshots()["items"] if s["business_date"] == "2026-07-02")
        saved_july = self.store.get_snapshot(july2["id"])
        self.assertFalse(saved_july["settings"]["bridge_m6"])
        self.assertEqual(saved_july["meta"]["months"][-1], "2026-12")

    def test_http_security_and_error_statuses(self):
        (self.root / "index.html").write_text("<html>local test</html>", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        server.RequestHandlerClass = make_handler(self.store, self.root, server.server_port)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(method, path, body, headers or {})
            result = connection.getresponse()
            status, data = result.status, result.read()
            connection.close()
            return status, data

        try:
            status, data = request("GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["plan_end"], "2026-12")
            self.assertEqual(request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(request("POST", "/api/plans", "{}", {"Content-Type": "application/json", "Origin": "https://evil.example"})[0], 403)
            self.assertEqual(request("POST", "/api/plans", "{}", {"Content-Type": "text/plain"})[0], 400)
            payload = json.dumps({"owner": "test", "changes": [self.plan()]})
            self.assertEqual(request("POST", "/api/plans", payload, {"Content-Type": "application/json"})[0], 200)
            self.assertEqual(request("POST", "/api/plans", payload, {"Content-Type": "application/json"})[0], 409)
            self.assertEqual(request("GET", "/../not-available")[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
