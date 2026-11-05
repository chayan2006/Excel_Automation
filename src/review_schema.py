"""Field definitions, value normalisation and validation for the invoice review workspace.

The schema lives in config/review_schema.json. Fields are grouped into sections (the review sidebar) and each
field has a type: string, number, date, enum, or table (rows of typed columns). Every extracted or typed value is
stored as a "state": the value plus where it was found and how sure the extractor was.
"""
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dateutil import parser as date_parser


GSTIN_RE = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DEFAULT_TOLERANCE = Decimal("0.5")


def load_schema(path: Path) -> dict:
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema.setdefault("checks", [])
    return schema


def iter_fields(schema: dict):
    """Yield every top-level field definition, tables included, in sidebar order."""
    for section in schema["sections"]:
        yield from section["fields"]


def field_definition(schema: dict, path: str) -> dict | None:
    """Look up a field ("invoice_number") or a table cell ("tax_details.0.amount") definition."""
    parts = path.split(".")
    for field in iter_fields(schema):
        if field["id"] != parts[0]:
            continue
        if len(parts) == 1:
            return field
        if len(parts) == 3 and field.get("type") == "table":
            return next((column for column in field["columns"] if column["id"] == parts[2]), None)
    return None


def blank_state(value: str = "", confidence: float | None = None, source: str | None = None) -> dict:
    return {
        "value": value,
        "raw": "",
        "page": None,
        "bbox": None,
        "confidence": confidence,
        "source": source,
        "validated": False,
    }


def parse_number(text: str) -> Decimal | None:
    cleaned = re.sub(r"(?i)inr|rs\.?|[₹`%\s,]", "", text)
    negative = cleaned.startswith("-") or cleaned.endswith("-") or (cleaned.startswith("(") and cleaned.endswith(")"))
    try:
        number = Decimal(cleaned.strip("-()"))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return -number if negative else number


def parse_date(text: str) -> date | None:
    text = text.strip()
    # dateutil happily turns "12" into the 12th of this month, so ask for day, month and year.
    if len(re.findall(r"[A-Za-z]+|\d+", text)) < 3:
        return None
    try:
        if ISO_DATE_RE.fullmatch(text):
            return date.fromisoformat(text)
        return date_parser.parse(text, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def match_option(options: list[str], text: str) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", text.lower())
    for option in options:
        if re.sub(r"[^a-z0-9]", "", option.lower()) == key:
            return option
    return None


def gstin_is_valid(value: str) -> bool:
    """GSTINs end with a Luhn mod-36 check character, which catches most single misread characters."""
    if not GSTIN_RE.fullmatch(value):
        return False
    total = 0
    for index, char in enumerate(value[:14]):
        product = GSTIN_CHARS.index(char) * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36] == value[14]


def normalize_value(definition: dict, raw) -> tuple[str, bool]:
    """Turn captured or typed text into the stored value and report whether it is valid for the field type.

    Text that cannot be read as the field's type is kept as-is, so the reviewer sees what was found.
    """
    text = "" if raw is None else str(raw)
    if definition.get("multiline"):
        text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    else:
        text = " ".join(text.split())
    if not text:
        return "", True

    kind = definition.get("type", "string")
    if kind == "number":
        number = parse_number(text)
        return (format(number, "f"), True) if number is not None else (text, False)
    if kind == "date":
        parsed = parse_date(text)
        return (parsed.isoformat(), True) if parsed else (text, False)
    if kind == "enum":
        option = match_option(definition.get("options", []), text)
        return (option, True) if option else (text, False)
    if definition.get("format") == "gstin":
        text = text.replace(" ", "").upper()
        return text, gstin_is_valid(text)
    return text, True


def value_problem(definition: dict, value: str) -> str | None:
    if not value:
        return None
    kind = definition.get("type", "string")
    if kind == "number" and parse_number(value) is None:
        return "Enter a number, for example 1234.50."
    if kind == "date" and parse_date(value) is None:
        return "Enter a valid date."
    if kind == "enum" and value not in definition.get("options", []):
        return "Choose one of the listed options."
    if definition.get("format") == "gstin" and not gstin_is_valid(value):
        if GSTIN_RE.fullmatch(value):
            return "The GSTIN check character does not match. Look for a misread character."
        return "A GSTIN has 15 characters, for example 27AAPFU0939F1ZV."
    return None


def typed_value(definition: dict, value: str):
    """Value converted for Excel: numbers as floats and dates as dates, anything unreadable as text."""
    if not value:
        return None
    kind = definition.get("type", "string")
    if kind == "number":
        number = parse_number(value)
        return float(number) if number is not None else value
    if kind == "date":
        return parse_date(value) or value
    return value


def _number(fields: dict, field_id: str) -> Decimal | None:
    value = (fields.get(field_id) or {}).get("value", "")
    return parse_number(value) if value else None


def run_check(check: dict, fields: dict, tables: dict) -> bool | None:
    """True or False when the check can be evaluated, None while one of its values is still missing."""
    target = _number(fields, check["equals"])
    if target is None:
        return None
    if check["type"] == "sum":
        total = Decimal(0)
        for name in check["fields"]:
            value = _number(fields, name.lstrip("-"))
            if value is None:
                return None
            total += -value if name.startswith("-") else value
    elif check["type"] == "table_sum":
        values = [
            parse_number((row.get(check["column"]) or {}).get("value", "") or "")
            for row in tables.get(check["table"], [])
        ]
        values = [value for value in values if value is not None]
        if not values:
            return None
        total = sum(values, Decimal(0))
    else:
        return None
    tolerance = Decimal(str(check.get("tolerance", DEFAULT_TOLERANCE)))
    return abs(total - target) <= tolerance


def validate(schema: dict, fields: dict, tables: dict, has_text: bool = True) -> list[dict]:
    """Messages shown in the sidebar. Errors block confirmation, warnings only ask for a second look."""
    messages = []

    def add(path, level, text):
        messages.append({"path": path, "level": level, "text": text})

    if not has_text:
        add(None, "warning", "This PDF has no text layer (it is probably a scan), so nothing could be extracted. Type the values in.")

    for field in iter_fields(schema):
        if field.get("type") == "table":
            for row_index, row in enumerate(tables.get(field["id"], [])):
                for column in field["columns"]:
                    problem = value_problem(column, (row.get(column["id"]) or {}).get("value", ""))
                    if problem:
                        add(f"{field['id']}.{row_index}.{column['id']}", "error", problem)
            continue
        value = (fields.get(field["id"]) or {}).get("value", "")
        if not value:
            if field.get("required"):
                add(field["id"], "error", "This field is required.")
            continue
        problem = value_problem(field, value)
        if problem:
            add(field["id"], "error", problem)

    for check in schema.get("checks", []):
        if run_check(check, fields, tables) is False:
            add(check.get("path", check["equals"]), "warning", check["message"])
    return messages


def _unsure(state: dict, threshold: float, required: bool) -> bool:
    if state.get("validated"):
        return False
    if not state.get("value"):
        return required
    return (state.get("confidence") or 0) < threshold


def review_queue(schema: dict, fields: dict, tables: dict, threshold: float) -> list[str]:
    """Paths of visible values a person still has to check, in sidebar order."""
    paths = []
    for field in iter_fields(schema):
        if field.get("hidden") or field.get("manual"):
            continue
        if field.get("type") == "table":
            for row_index, row in enumerate(tables.get(field["id"], [])):
                for column in field["columns"]:
                    if _unsure(row.get(column["id"]) or {}, threshold, False):
                        paths.append(f"{field['id']}.{row_index}.{column['id']}")
        elif _unsure(fields.get(field["id"]) or {}, threshold, bool(field.get("required"))):
            paths.append(field["id"])
    return paths
