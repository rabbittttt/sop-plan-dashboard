"""Isolated continuous-layout visual QA; never opens the production database/workbook."""
import json
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import openpyxl

from sop_store import SOPStore, TZ
from sop_workbook import WorkbookSync


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "tests/artifacts/continuous-layout"
    output.mkdir(parents=True, exist_ok=True)
    sales = json.loads((root / "data/sales.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        source = temp / "sales.json"
        source.write_text(json.dumps(sales,ensure_ascii=False),encoding="utf-8")
        store = SOPStore(temp / "test.sqlite3",source,lambda:datetime(2026,10,23,12,tzinfo=TZ))
        try:
            sync = WorkbookSync(store,temp / "test.xlsx")
            context,_ = sync._context()
            target = output / "SOP计划维护-样式核验.xlsx"
            token = str(uuid4())
            payload={**context,"token":token,"generated_at":"2026-10-23T12:00:00+08:00",
                     "output_path":str(target),"preview_path":str(output / "continuous-preview.png")}
            sync._node_writer(payload)
            store.db.execute("INSERT INTO workbook_exports VALUES (?,?,?)",(token,payload["generated_at"],json.dumps(context)))
            values=sync._parse(target.read_bytes())[-1]
            assert all(value is None for value in values.values()),"A blank plan was populated during layout migration"
            workbook=openpyxl.load_workbook(target,read_only=True,data_only=True)
            try:
                for spec in context["layout"]["sheets"]:
                    for row in workbook[spec["name"]].iter_rows():
                        assert not any(cell.data_type=="e" for cell in row),"Formula error in export"
                    assert workbook[spec["name"]][f"P{spec['last_row']}"].value in (None,"")
            finally:
                workbook.close()
            print(json.dumps({"output":str(target),"products":len(context["products"]),
                              "display_months":len(context["months"]),"blank_inputs":len(values),
                              "production_writes":False},ensure_ascii=False))
        finally:
            store.close()


if __name__=="__main__":
    main()
