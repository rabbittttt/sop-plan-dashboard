"""Shared paths for the editable plan and raw sales workbooks."""

from pathlib import Path


PLAN_WORKBOOK_NAME = "SOP计划维护.xlsx"


def plan_workbook_path(root):
    return Path(root) / "input_file" / PLAN_WORKBOOK_NAME


def sales_source_files(source_dir):
    """Return raw sales files; the editable SOP workbook is not a sales source."""
    return sorted(
        path for path in Path(source_dir).glob("*.xlsx")
        if not path.name.startswith("~$")
        and path.name.casefold() != PLAN_WORKBOOK_NAME.casefold()
        and not path.name.lower().endswith(".pending.xlsx")
    )
