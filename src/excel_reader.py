import re
from pathlib import Path

import pandas as pd


def normalize_column_name(value) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip().replace(" ", "_")


class ExcelReader:
    def __init__(self, config: dict):
        self.config = config

    def determine_sheet_name(self, workbook: pd.ExcelFile) -> str:
        configured = self.config.get("sheet_name")
        if configured and configured in workbook.sheet_names:
            return configured

        candidates = self.config.get("sheet_candidates", [])
        for candidate in candidates:
            if candidate in workbook.sheet_names:
                return candidate

        if workbook.sheet_names:
            return workbook.sheet_names[0]

        raise ValueError("No worksheets found in the Excel file.")

    def read_excel(self, file_path: Path):
        with pd.ExcelFile(file_path) as workbook:
            sheet_name = self.determine_sheet_name(workbook)
            # Keep text identifiers such as "0012" intact, including columns with empty cells.
            df = pd.read_excel(workbook, sheet_name=sheet_name, dtype=object)

        df = df.reset_index(drop=True)
        alias_map = {
            normalize_column_name(key): value
            for key, value in self.config.get("column_aliases", {}).items()
        }

        renamed = {}
        for column in df.columns:
            raw_name = str(column).strip()
            alias_target = alias_map.get(normalize_column_name(raw_name), raw_name)
            renamed[column] = alias_target

        df = df.rename(columns=renamed)
        return df, sheet_name
