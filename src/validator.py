import re
from typing import Any

import pandas as pd


def canonicalize(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def validate_filename(file_name: str, pattern: str | None) -> str | None:
    if not pattern:
        return None
    if re.fullmatch(pattern, file_name) is None:
        return f"Filename '{file_name}' does not match the required pattern: {pattern}"
    return None


def normalize_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized_df = df.copy()
    normalized_df.columns = [str(column).strip() for column in normalized_df.columns]
    return normalized_df


def clean_dataframe(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    cleaned = normalize_dataframe_columns(df)

    for column in cleaned.columns:
        cleaned[column] = cleaned[column].map(lambda value: "" if pd.isna(value) else str(value).strip())

    if config.get("business_rules", {}).get("skip_blank_rows", False):
        cleaned = cleaned.loc[cleaned.ne("").any(axis=1)].copy()

    if "Date" in cleaned.columns:
        cleaned["Date"] = pd.to_datetime(cleaned["Date"], errors="coerce")

    return cleaned


def validate_dataframe(df: pd.DataFrame, config: dict, file_name: str, sheet_name: str) -> tuple[pd.DataFrame, list[dict]]:
    errors: list[dict] = []
    normalized = normalize_dataframe_columns(df)
    duplicates = normalized.columns[normalized.columns.duplicated()].unique()
    if len(duplicates):
        return normalized, [{
            "filename": file_name, "sheet": sheet_name, "row": None,
            "column": column, "error_type": "duplicate_column",
            "error_message": f"More than one column maps to '{column}'. Use unique column names.",
        } for column in duplicates]
    cleaned = clean_dataframe(normalized, config)
    required_columns = config.get("required_columns", [])

    for column in required_columns:
        if column not in cleaned.columns:
            errors.append(
                {
                    "filename": file_name,
                    "sheet": sheet_name,
                    "row": None,
                    "column": column,
                    "error_type": "missing_required_column",
                    "error_message": f"Missing required column: {column}",
                }
            )

    if cleaned.empty:
        errors.append(
            {
                "filename": file_name,
                "sheet": sheet_name,
                "row": None,
                "column": None,
                "error_type": "empty_sheet",
                "error_message": "No rows found in the worksheet.",
            }
        )
        return cleaned, errors

    if not required_columns:
        return cleaned, errors

    existing_required = [column for column in required_columns if column in cleaned.columns]
    if not existing_required:
        return cleaned, errors

    for row_index, row in cleaned.iterrows():
        relevant_values = row[existing_required]
        empty_row = relevant_values.map(lambda value: canonicalize(value) == "").all()
        if empty_row:
            errors.append(
                {
                    "filename": file_name,
                    "sheet": sheet_name,
                    "row": row_index + 2,
                    "column": None,
                    "error_type": "empty_row",
                    "error_message": "Blank row detected and skipped.",
                }
            )
            continue

        for column in existing_required:
            value = row.get(column)
            if canonicalize(value) == "":
                errors.append(
                    {
                        "filename": file_name,
                        "sheet": sheet_name,
                        "row": row_index + 2,
                        "column": column,
                        "error_type": "missing_value",
                        "error_message": f"Missing required value in column '{column}'.",
                    }
                )

    identifier_columns = config.get("identifier_columns", [])
    if identifier_columns and all(column in cleaned.columns for column in identifier_columns):
        keys = cleaned[identifier_columns].apply(lambda column: column.map(canonicalize))
        complete_keys = keys.loc[keys.ne("").all(axis=1)]
        duplicates = complete_keys.loc[complete_keys.duplicated(keep=False)]
        if not duplicates.empty:
            for values in duplicates.drop_duplicates().itertuples(index=False, name=None):
                identifier = ", ".join(identifier_columns)
                value = ", ".join(values)
                errors.append(
                    {
                        "filename": file_name,
                        "sheet": sheet_name,
                        "row": None,
                        "column": identifier,
                        "error_type": "duplicate_record",
                        "error_message": f"Duplicate record detected for '{identifier}': {value}",
                    }
                )

    if "Date" in cleaned.columns:
        invalid_dates = cleaned["Date"].isna()
        invalid_date_rows = cleaned.index[invalid_dates].tolist()
        for row_index in invalid_date_rows:
            errors.append(
                {
                    "filename": file_name,
                    "sheet": sheet_name,
                    "row": row_index + 2,
                    "column": "Date",
                    "error_type": "invalid_date",
                    "error_message": "Date value is not a valid date.",
                }
            )

    return cleaned, errors
