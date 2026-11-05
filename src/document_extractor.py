"""Position-aware invoice extraction for the review workspace.

invoice_extractor.extract_pdf only returns values. This module also keeps where each value was found (page number
and bounding box in PDF points, origin at the top-left of the page) so the review screen can highlight it, and it
scores every value by how it was found so reviewers only have to look at the uncertain ones.

PDFium (pypdfium2) is used where speed matters: page sizes, a quick text scan of many pages, and page images.
pdfplumber is used where positions matter: regex search with bounding boxes on the few pages that need it.
"""
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

try:
    from .invoice_extractor import DATE_PATTERN, GSTIN_PATTERN, classify_vendor
    from .review_schema import blank_state, iter_fields, normalize_value, run_check, value_problem
except ImportError:  # pragma: no cover
    from invoice_extractor import DATE_PATTERN, GSTIN_PATTERN, classify_vendor
    from review_schema import blank_state, iter_fields, normalize_value, run_check, value_problem


# PDFium is not thread-safe: every call into it must hold this lock, even for different documents.
PDFIUM_LOCK = threading.Lock()

TEMPLATE_SCORE = 0.95  # matched by a rule written for this vendor's bill layout
UNVERIFIED_SCORE = 0.85  # matched by a vendor rule that has not been checked against real bills yet
GENERIC_SCORE = 0.7  # matched by a pattern meant to work on any invoice
INVALID_SCORE = 0.3  # matched, but the text is not a valid value for the field
CHECKED_SCORE = 0.99  # matched and confirmed by an arithmetic check such as subtotal + tax = total

HEADER_PAGES = 2
AMOUNT = r"-?[0-9][0-9,]*\.\d{2}"
LOOSE_AMOUNT = r"[0-9][0-9,]*(?:\.\d{1,2})?"
LEFT_COLUMN = (0.0, 0.0, 0.55, 0.45)


@dataclass(frozen=True)
class Rule:
    pattern: str  # the value is capture group 1
    keyword: str | None = None  # search every scanned page containing this text instead of the header pages only
    region: tuple[float, float, float, float] | None = None  # (x0, top, x1, bottom) page fractions to search in
    value: str | None = None  # store this instead of the matched text, e.g. "INR" when "Rupees" is found
    flags: int = re.IGNORECASE


# Written against Airtel enterprise mobile bills (voice and data), where the header block, account summary and
# "Tax Details" table share one layout no matter how many connections the bill covers.
AIRTEL_RULES = {
    "document_type": [Rule(r"Recipient\s*-\s*(Tax\s+Invoice)")],
    "invoice_number": [Rule(r"Bill\s+number\s+([A-Z0-9]{6,})")],
    "invoice_date": [Rule(rf"Bill\s+date\s+({DATE_PATTERN})")],
    "billing_period": [Rule(rf"Bill\s+period\s+({DATE_PATTERN}\s+to\s+{DATE_PATTERN})")],
    "due_date": [Rule(rf"Pay\s+by\s+date\s+(immediately|{DATE_PATTERN})")],
    "irn": [Rule(r"IRN\s+Code\s*:\s*([0-9a-f]{64})")],
    "account_number": [Rule(r"Relationship\s+number\s+(\d{6,})")],
    "place_of_supply": [Rule(r"POS\s*:\s*([A-Za-z][A-Za-z ]*?)\s+Security")],
    "vendor_bank_account": [Rule(r"Bank\s+A/c\s+No\s*:\s*([A-Z0-9]{6,})")],
    "vendor_ifsc": [Rule(r"IFSC\s+Code\s*:\s*([A-Z]{4}0[A-Z0-9]{6})")],
    "previous_balance": [Rule(rf"Previous\s+balance\s+({AMOUNT})")],
    "payments_received": [Rule(rf"Payments\s+-\s+({AMOUNT})")],
    "adjustments": [Rule(rf"Adjustments\s+-\s+({AMOUNT})")],
    "amount_total_base": [
        Rule(rf"Tax\s+Details.*?^\s*\d{{4,8}}\s+({AMOUNT})\s", keyword="Tax Details", flags=re.I | re.S | re.M)
    ],
    "amount_total_tax": [Rule(rf"Taxes\s+({AMOUNT})")],
    "amount_total": [Rule(rf"This\s+month.s\s+charges\s+\+\s+({AMOUNT})")],
    "amount_due": [Rule(rf"Amount\s+due[^=]{{0,40}}=\s*({AMOUNT})")],
    "currency": [Rule(r"\b(Rupees)\b", value="INR")],
    "vendor_name": [Rule(r"For\s+(Bharti\s+Airtel\s+Limited)")],
    "vendor_gstin": [Rule(rf"GST\s+Reg\s+No\s*:?\s*({GSTIN_PATTERN})")],
    "vendor_pan": [Rule(r"PAN\s*:\s*([A-Z]{5}[0-9]{4}[A-Z])")],
    "customer_name": [Rule(r"Recipient\s*-\s*Tax\s+Invoice\s*\n(?:[.\s]+\n)?([^\n]+)", region=LEFT_COLUMN)],
    "customer_gstin": [Rule(rf"GST\s+No/UIN\s+No\s+({GSTIN_PATTERN})")],
    "customer_address": [
        Rule(
            r"Recipient\s*-\s*Tax\s+Invoice\s*\n(?:[.\s]+\n)?[^\n]+\n(.+?)\n\s*(?:Landmark|POS\s*:|Email\s+ID)",
            region=LEFT_COLUMN,
            flags=re.I | re.S,
        )
    ],
}

# Carried over from the original Jio and Vodafone Idea extractors.
JIO_RULES = {
    "invoice_number": [Rule(r"Invoice\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)")],
    "invoice_date": [Rule(rf"Bill\s*Date\s*[:.\-\s]+({DATE_PATTERN})")],
    "account_number": [Rule(r"Customer\s*ID\s*[:.\-\s]+([A-Z0-9/-]+)")],
    "amount_due": [Rule(r"Total\s*Amount\s*Due\s*[:.\-\s]+(?:₹|Rs\.?)?\s*([0-9,.]+)")],
    "vendor_gstin": [Rule(rf"GSTIN\s*[:.\-\s]+({GSTIN_PATTERN})")],
}

VI_RULES = {
    "invoice_number": [Rule(r"Invoice\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)")],
    "invoice_date": [Rule(rf"Invoice\s*Date\s*[:.\-\s]+({DATE_PATTERN})")],
    "account_number": [Rule(r"Account\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)")],
    "amount_due": [Rule(r"Total\s*Amount\s*Payable\s*[:.\-\s]+(?:₹|Rs\.?)?\s*([0-9,.]+)")],
    "vendor_gstin": [Rule(rf"GSTIN\s*[:.\-\s]+({GSTIN_PATTERN})")],
}

GENERIC_RULES = {
    "document_type": [Rule(r"\b(Tax\s+Invoice|Credit\s+Note|Debit\s+Note|Pro[\s-]*forma\s+Invoice|Receipt)\b")],
    "invoice_number": [Rule(r"(?:Invoice|Bill)\s*(?:No\.?|Number|#)\s*[:.\-]?\s*((?=[A-Z/-]*\d)[A-Z0-9][A-Z0-9/-]{3,})")],
    "invoice_date": [Rule(rf"(?:Invoice|Bill)\s*Date\s*[:.\-]?\s*({DATE_PATTERN})")],
    "due_date": [Rule(rf"(?:Due\s*Date|Pay\s*by(?:\s*date)?)\s*[:.\-]?\s*({DATE_PATTERN}|immediately)")],
    "account_number": [Rule(r"(?:Account|Relationship|Customer)\s*(?:No\.?|Number|ID)\s*[:.\-]?\s*([0-9][A-Z0-9/-]{3,})")],
    "amount_total_tax": [Rule(rf"(?:Total\s*Tax|Tax\s*Amount)\s*[:.\-]?\s*(?:INR|Rs\.?|₹|`)?\s*({AMOUNT})")],
    "amount_due": [
        Rule(
            r"(?:Total\s*(?:Amount\s*)?(?:Payable|Due)|Amount\s*(?:Due|Payable))[^0-9\n=]{0,30}[=:]?\s*"
            rf"(?:INR|Rs\.?|₹|`)?\s*({LOOSE_AMOUNT})"
        )
    ],
    "vendor_gstin": [Rule(rf"GSTIN\s*[:.\-]?\s*({GSTIN_PATTERN})")],
    "currency": [Rule(r"(₹|\bINR\b|\bRs\.|\bRupees\b)", value="INR")],
}

VENDOR_TEMPLATES = {
    "Airtel": (AIRTEL_RULES, TEMPLATE_SCORE),
    # Not yet checked against real Jio and Vodafone Idea bills, so their matches still go to review.
    "Jio": (JIO_RULES, UNVERIFIED_SCORE),
    "Vodafone Idea": (VI_RULES, UNVERIFIED_SCORE),
}

VENDOR_NAMES = {
    "Airtel": "Bharti Airtel Limited",
    "Jio": "Reliance Jio Infocomm Limited",
    "Vodafone Idea": "Vodafone Idea Limited",
}

AIRTEL_TAX_HEADER = r"(CGST)\s+(SGST(?:/UTGST)?)"
AIRTEL_TAX_ROW = (
    r"^\s*(\d{4,8})\s+([0-9][0-9.]*)\s+(\d+(?:\.\d+)?)%\s+([0-9][0-9.]*)\s+(\d+(?:\.\d+)?)%\s+([0-9][0-9.]*)\s+[0-9]"
)
AIRTEL_IGST_ROW = r"^\s*(\d{4,8})\s+([0-9][0-9.]*)\s+(\d+(?:\.\d+)?)%\s+([0-9][0-9.]*)\s+[0-9][0-9.]*\s+[0-9][0-9.]*\s*$"
AIRTEL_CHARGES = ("Monthly rentals", "Usage", "One time charges", "Discounts", "Late payment charges")


def _page_text(document, index: int) -> str:
    page = document[index]
    try:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        page.close()


class PdfReader:
    """A PDF opened once for extraction, with the per-page work cached."""

    def __init__(self, path: Path, max_scan_pages: int):
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(str(path))
            try:
                self.page_sizes = [document.get_page_size(index) for index in range(len(document))]
                scan_count = min(len(document), max_scan_pages)
                self.texts = [_page_text(document, index) for index in range(scan_count)]
            finally:
                document.close()
        self.has_text = any(len(text.strip()) > 20 for text in self.texts[:HEADER_PAGES])
        self.pdf = pdfplumber.open(path)
        self._pages = {}

    def close(self) -> None:
        self.pdf.close()

    def pages_with(self, keyword: str) -> list[int]:
        keyword = keyword.lower()
        return [number for number, text in enumerate(self.texts, start=1) if keyword in text.lower()]

    def candidate_pages(self, rule: Rule) -> list[int]:
        if rule.keyword:
            return self.pages_with(rule.keyword)
        return list(range(1, min(HEADER_PAGES, len(self.texts)) + 1))

    def page(self, number: int, region=None):
        if (number, region) not in self._pages:
            if (number, None) not in self._pages:
                # Some bills draw bold text twice ("PPaaggee 11"); dedupe so patterns see the real text.
                self._pages[(number, None)] = self.pdf.pages[number - 1].dedupe_chars()
            page = self._pages[(number, None)]
            if region:
                x0, top, x1, bottom = region
                page = page.crop((x0 * page.width, top * page.height, x1 * page.width, bottom * page.height))
            self._pages[(number, region)] = page
        return self._pages[(number, region)]

    def search(self, number: int, pattern: str, flags=re.IGNORECASE, region=None, group: int = 1) -> list[dict]:
        matches = self.page(number, region).search(re.compile(pattern, flags), main_group=group, return_chars=False)
        return [
            {"text": match["text"], "page": number, "bbox": [round(match[key], 2) for key in ("x0", "top", "x1", "bottom")]}
            for match in matches
        ]

    def search_groups(self, number: int, pattern: str, count: int, flags=re.IGNORECASE) -> list[tuple]:
        """Every match of the pattern as a tuple with one located value per capture group."""
        columns = [self.search(number, pattern, flags, group=group) for group in range(1, count + 1)]
        return list(zip(*columns))


def located_state(definition: dict, match: dict, score: float, source: str, value: str | None = None) -> dict:
    normalized, ok = normalize_value(definition, match["text"] if value is None else value)
    ok = ok and value_problem(definition, normalized) is None
    return {
        "value": normalized,
        "raw": match["text"],
        "page": match["page"],
        "bbox": match["bbox"],
        "confidence": score if ok else INVALID_SCORE,
        "source": source,
        "validated": False,
    }


def default_state(field: dict) -> dict:
    return blank_state(field["default"], None, "default") if field.get("default") else blank_state()


def find_field(reader: PdfReader, vendor: str, field: dict) -> dict:
    vendor_rules, vendor_score = VENDOR_TEMPLATES.get(vendor, ({}, TEMPLATE_SCORE))
    for rules, score, source in ((vendor_rules, vendor_score, "template"), (GENERIC_RULES, GENERIC_SCORE, "generic")):
        for rule in rules.get(field["id"], ()):
            for number in reader.candidate_pages(rule):
                matches = reader.search(number, rule.pattern, rule.flags, rule.region)
                if matches:
                    return located_state(field, matches[0], score, source, rule.value)
    if field["id"] == "vendor_name" and vendor in VENDOR_NAMES:
        return blank_state(VENDOR_NAMES[vendor], GENERIC_SCORE, "detected")
    return default_state(field)


def _table_row(table: dict, **cells) -> dict:
    """Build a table row from {column_id: (match, value_override)}; missing columns stay blank."""
    columns = {column["id"]: column for column in table["columns"]}
    row = {column_id: blank_state() for column_id in columns}
    for column_id, (match, value) in cells.items():
        if match is not None:
            row[column_id] = located_state(columns[column_id], match, TEMPLATE_SCORE, "template", value)
        elif value is not None:
            row[column_id] = blank_state(value, TEMPLATE_SCORE, "template")
    return row


def airtel_tables(reader: PdfReader, tables: dict) -> dict:
    found = {}
    if "tax_details" in tables:
        rows = []
        for number in reader.pages_with("Tax Details"):
            header = reader.search_groups(number, AIRTEL_TAX_HEADER, 2)
            cgst_head, sgst_head = header[0] if header else (None, None)
            for hsn, base, cgst_rate, cgst, sgst_rate, sgst in reader.search_groups(number, AIRTEL_TAX_ROW, 6, re.I | re.M):
                rows.append(_table_row(tables["tax_details"], tax_type=(cgst_head, "CGST"), hsn=(hsn, None),
                                       rate=(cgst_rate, None), base=(base, None), amount=(cgst, None)))
                rows.append(_table_row(tables["tax_details"], tax_type=(sgst_head, "SGST"), hsn=(hsn, None),
                                       rate=(sgst_rate, None), base=(base, None), amount=(sgst, None)))
            if not rows:
                for hsn, base, rate, igst in reader.search_groups(number, AIRTEL_IGST_ROW, 4, re.I | re.M):
                    rows.append(_table_row(tables["tax_details"], tax_type=(None, "IGST"), hsn=(hsn, None),
                                           rate=(rate, None), base=(base, None), amount=(igst, None)))
            if rows:
                break
        found["tax_details"] = rows

    if "line_items" in tables and reader.texts:
        rows = []
        for label in AIRTEL_CHARGES:
            label_pattern = label.replace(" ", r"\s+")
            for description, amount in reader.search_groups(1, rf"\b({label_pattern})\s+({AMOUNT})", 2):
                rows.append(_table_row(tables["line_items"], description=(description, None), amount=(amount, None)))
        rows.sort(key=lambda row: row["description"]["bbox"][1])
        found["line_items"] = rows
    return found


TABLE_EXTRACTORS = {"Airtel": airtel_tables}


def confirm_with_checks(schema: dict, fields: dict, tables: dict) -> None:
    """Raise the confidence of extracted values that an arithmetic check proves consistent."""
    for check in schema.get("checks", []):
        if run_check(check, fields, tables) is not True:
            continue
        states = [fields.get(name.lstrip("-")) for name in check.get("fields", [])] + [fields.get(check["equals"])]
        if check["type"] == "table_sum":
            states += [row.get(check["column"]) for row in tables.get(check["table"], [])]
        for state in states:
            if state and state.get("source") in ("template", "generic") and (state.get("confidence") or 0) > INVALID_SCORE:
                state["confidence"] = CHECKED_SCORE


def extract_document(pdf_path: Path, schema: dict, max_scan_pages: int = 60) -> dict:
    reader = PdfReader(pdf_path, max_scan_pages)
    try:
        vendor = classify_vendor("\n".join(reader.texts[:HEADER_PAGES]))
        fields, tables, table_definitions = {}, {}, {}
        for field in iter_fields(schema):
            if field.get("type") == "table":
                tables[field["id"]] = []
                table_definitions[field["id"]] = field
            elif field.get("manual") or not reader.has_text:
                fields[field["id"]] = default_state(field)
            else:
                fields[field["id"]] = find_field(reader, vendor, field)
        if reader.has_text and vendor in TABLE_EXTRACTORS:
            tables.update(TABLE_EXTRACTORS[vendor](reader, table_definitions))
        confirm_with_checks(schema, fields, tables)
        return {
            "vendor": vendor,
            "has_text": reader.has_text,
            "page_count": len(reader.page_sizes),
            "pages": [[round(width, 2), round(height, 2)] for width, height in reader.page_sizes],
            "fields": fields,
            "tables": tables,
        }
    finally:
        reader.close()


def render_page(pdf_path: Path, page_number: int, destination: Path, resolution: int) -> Path:
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            if not 1 <= page_number <= len(document):
                raise IndexError(f"Page {page_number} does not exist.")
            page = document[page_number - 1]
            try:
                image = page.render(scale=resolution / 72).to_pil()
            finally:
                page.close()
        finally:
            document.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write to a unique temporary file first so a request for the same page never reads a half-written image.
    temporary = destination.with_name(f"{destination.stem}.{uuid.uuid4().hex}.tmp")
    image.convert("RGB").quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(temporary, format="PNG")
    temporary.replace(destination)
    return destination


def capture_text(pdf_path: Path, page_number: int, bbox: list[float]) -> tuple[str, list[float] | None]:
    """Text of the words inside a box drawn on the page, and the box shrunk to fit them.

    A word counts as inside when its centre is, so a box that clips the edge of a neighbouring label does not
    pull in half a word.
    """
    x0, x1 = sorted((bbox[0], bbox[2]))
    y0, y1 = sorted((bbox[1], bbox[3]))
    with pdfplumber.open(pdf_path) as pdf:
        if not 1 <= page_number <= len(pdf.pages):
            raise IndexError(f"Page {page_number} does not exist.")
        words = pdf.pages[page_number - 1].dedupe_chars().extract_words()
    inside = [
        word for word in words
        if x0 <= (word["x0"] + word["x1"]) / 2 <= x1 and y0 <= (word["top"] + word["bottom"]) / 2 <= y1
    ]
    if not inside:
        return "", None

    lines = []
    for word in sorted(inside, key=lambda item: (item["top"], item["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= 3:
            lines[-1].append(word)
        else:
            lines.append([word])
    text = "\n".join(" ".join(word["text"] for word in sorted(line, key=lambda item: item["x0"])) for line in lines)
    return text, [
        round(min(word["x0"] for word in inside), 2),
        round(min(word["top"] for word in inside), 2),
        round(max(word["x1"] for word in inside), 2),
        round(max(word["bottom"] for word in inside), 2),
    ]
