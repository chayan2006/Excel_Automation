"""Excel export of reviewed documents: one row per document, plus one sheet per table field."""
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

try:
    from .review_schema import iter_fields, typed_value
except ImportError:  # pragma: no cover
    from review_schema import iter_fields, typed_value


EXPORT_NAME_PATTERN = r"review_export_\d{8}_\d{6}(?:_\d+)?\.xlsx"


def write_sheet(writer: pd.ExcelWriter, name: str, frame: pd.DataFrame) -> None:
    frame.to_excel(writer, sheet_name=name, index=False)
    sheet = writer.sheets[name]
    sheet.freeze_panes = "A2"
    for index, column in enumerate(frame.columns, start=1):
        longest = max([len(str(column))] + [len(str(value)) for value in frame[column].tolist()[:200]])
        sheet.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 10), 60)


def export_documents(documents: list[dict], schema: dict, queue_names: dict, reports_dir: Path) -> Path:
    fields = [field for field in iter_fields(schema) if field.get("type") != "table"]
    tables = [field for field in iter_fields(schema) if field.get("type") == "table"]

    summary_rows = []
    table_rows = {table["id"]: [] for table in tables}
    for document in documents:
        reference = {"Document ID": document["id"], "File": document["file_name"]}
        row = {
            **reference,
            "Queue": queue_names.get(document["queue"], document["queue"]),
            "Vendor (detected)": document.get("vendor"),
        }
        for field in fields:
            row[field["label"]] = typed_value(field, (document["fields"].get(field["id"]) or {}).get("value", ""))
        row["Labels"] = ", ".join(document.get("labels", []))
        row["Confirmed at"] = document.get("confirmed_at")
        row["Confirmed by"] = document.get("confirmed_by")
        summary_rows.append(row)

        for table in tables:
            for number, cells in enumerate(document["tables"].get(table["id"], []), start=1):
                values = {
                    column["label"]: typed_value(column, (cells.get(column["id"]) or {}).get("value", ""))
                    for column in table["columns"]
                }
                table_rows[table["id"]].append({**reference, "Row": number, **values})

    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = reports_dir / f"review_export_{timestamp}.xlsx"
    counter = 1
    while output_path.exists():
        output_path = reports_dir / f"review_export_{timestamp}_{counter}.xlsx"
        counter += 1

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        write_sheet(writer, "Documents", pd.DataFrame(summary_rows))
        for table in tables:
            columns = ["Document ID", "File", "Row", *[column["label"] for column in table["columns"]]]
            write_sheet(writer, table["label"][:31], pd.DataFrame(table_rows[table["id"]], columns=columns))
    return output_path
