import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
from openpyxl import Workbook, load_workbook


class MasterUpdater:
    def __init__(self, config: dict):
        self.config = config

    def ensure_master_workbook(self, master_path: Path, sheet_name: str) -> Workbook:
        if not master_path.exists():
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = sheet_name
            required_columns = self.config.get("required_columns", [])
            for index, column in enumerate(required_columns, start=1):
                worksheet.cell(row=1, column=index, value=column)
            workbook.save(master_path)
            return workbook
        return load_workbook(master_path, read_only=False, data_only=False)

    @staticmethod
    def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        normalized_df = df.copy()
        normalized_df.columns = [str(column).strip() for column in normalized_df.columns]
        return normalized_df

    def ensure_master_columns(self, worksheet, required_columns: list[str]) -> None:
        # Ignore formatted trailing cells when locating the final header.
        headers = [cell.value for cell in worksheet[1]]
        while headers and headers[-1] is None:
            headers.pop()
        normalized = [str(value).strip() if value is not None else "" for value in headers]
        populated = [value for value in normalized if value]
        if len(populated) != len(set(populated)):
            raise ValueError("The master worksheet has duplicate column names.")
        for column in required_columns:
            if column not in normalized:
                worksheet.cell(row=1, column=len(headers) + 1, value=column)
                headers.append(column)
                normalized.append(column)

    def _row_key(self, row, identifier_columns: list[str]) -> tuple[str, ...]:
        return tuple("" if pd.isna(row.get(column, "")) else str(row.get(column, "")).strip()
                     for column in identifier_columns)

    @staticmethod
    def _excel_value(value):
        return None if pd.isna(value) else value

    def update_master(self, master_path: Path, incoming_df: pd.DataFrame, file_name: str, change_details: list | None = None):
        sheet_name = self.config.get("sheet_name")
        identifier_columns = self.config.get("identifier_columns", ["Employee ID"])
        update_columns = self.config.get("business_rules", {}).get("update_columns", [])
        if not identifier_columns:
            raise ValueError("Configure at least one identifier column before updating the master.")

        workbook = load_workbook(master_path, read_only=False, data_only=False)
        temporary_path = None
        try:
            worksheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.create_sheet(title=sheet_name)
            self.ensure_master_columns(worksheet, list(self.config.get("required_columns", [])))
            columns = {str(cell.value).strip(): cell.column for cell in worksheet[1] if cell.value is not None}
            if any(column not in columns for column in identifier_columns):
                raise ValueError("The master worksheet is missing identifier columns.")
            incoming_df = self.normalize_columns(incoming_df)

            # Store actual Excel row numbers: a dataframe index loses gaps in the worksheet.
            existing_map = {}
            for cells in worksheet.iter_rows(min_row=2):
                values = {column: cells[index - 1].value for column, index in columns.items()}
                key = self._row_key(values, identifier_columns)
                if all(key):
                    if key in existing_map:
                        raise ValueError(f"Duplicate identifier in master workbook: {key}")
                    existing_map[key] = cells[0].row

            inserted_count = updated_count = skipped_count = 0
            for _, row in incoming_df.iterrows():
                key = self._row_key(row, identifier_columns)
                if not all(key):
                    raise ValueError("An incoming record is missing an identifier.")
                if key in existing_map:
                    row_index = existing_map[key]
                    changes = {}
                    for column in update_columns:
                        if column not in columns or column not in row:
                            continue
                        value = self._excel_value(row[column])
                        current = worksheet.cell(row=row_index, column=columns[column]).value
                        if ("" if current is None else str(current).strip()) != ("" if value is None else str(value).strip()):
                            changes[column] = value
                    if not changes:
                        skipped_count += 1
                        continue
                    if change_details is not None and len(change_details) < 200:
                        change_details.append({
                            "kind": "Update", "row": row_index, "key": " / ".join(key),
                            "values": [{"column": column,
                                        "before": worksheet.cell(row=row_index, column=columns[column]).value,
                                        "after": value} for column, value in changes.items()],
                        })
                    for column, value in changes.items():
                        worksheet.cell(row=row_index, column=columns[column]).value = value
                    updated_count += 1
                else:
                    row_index = worksheet.max_row + 1
                    if change_details is not None and len(change_details) < 200:
                        change_details.append({
                            "kind": "Add", "row": row_index, "key": " / ".join(key),
                            "values": [{"column": column, "before": None,
                                        "after": self._excel_value(row.get(column))} for column in columns],
                        })
                    for column, index in columns.items():
                        worksheet.cell(row=row_index, column=index).value = self._excel_value(row.get(column))
                    existing_map[key] = row_index
                    inserted_count += 1

            # A failed save must leave the live workbook intact.
            with NamedTemporaryFile(dir=master_path.parent, suffix=master_path.suffix, delete=False) as temporary:
                temporary_path = Path(temporary.name)
            workbook.save(temporary_path)
            os.replace(temporary_path, master_path)
        finally:
            workbook.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return {
            "file_name": file_name,
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
        }
