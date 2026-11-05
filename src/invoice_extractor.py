import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber


GSTIN_PATTERN = r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]"
DATE_PATTERN = r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4})"


def normalized_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\xa0", " ")).strip()


def search_pattern(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def classify_vendor(text: str) -> str:
    lowered = text.lower()
    if "bharti airtel" in lowered or "airtel" in lowered:
        return "Airtel"
    if "reliance jio" in lowered or "jio infocomm" in lowered or re.search(r"\bjio\b", lowered):
        return "Jio"
    if "vodafone idea" in lowered or "vi enterprise" in lowered or re.search(r"\bvodafone\b", lowered):
        return "Vodafone Idea"
    return "Unknown"


def amount_value(value: str | None) -> float | None:
    return float(value.replace(",", "")) if value else None


def extract_airtel(text: str) -> dict:
    # Airtel enterprise bills print "Bill number", "Bill date", "Relationship number", "Amount due immediately = ..."
    # and the vendor's "GST Reg No"; the Invoice/GSTIN spellings are kept for other Airtel layouts.
    return {
        "Vendor": "Airtel",
        "Invoice_No": search_pattern(r"(?:Invoice|Bill)\s*(?:No|Number)\s*[:.\-\s]+([A-Z0-9][A-Z0-9/-]+)", text),
        "Invoice_Date": search_pattern(rf"(?:Invoice|Bill)\s*Date\s*[:.\-\s]+({DATE_PATTERN})", text),
        "Account_No": search_pattern(r"(?:Account|Relationship)\s*(?:No\.?|Number)\s*[:.\-\s]+([0-9][A-Z0-9/-]*)", text),
        "Total_Amount": amount_value(search_pattern(r"(?:Total\s*Payable|Amount\s*Due)[^0-9=]{0,40}[=:]?\s*(?:₹|Rs\.?|`)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text)),
        "GSTIN": search_pattern(rf"(?:GSTIN|GST\s*Reg\s*No)\s*[:.\-\s]+({GSTIN_PATTERN})", text),
    }


def extract_jio(text: str) -> dict:
    return {
        "Vendor": "Jio",
        "Invoice_No": search_pattern(r"Invoice\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)", text),
        "Invoice_Date": search_pattern(rf"Bill\s*Date\s*[:.\-\s]+({DATE_PATTERN})", text),
        "Account_No": search_pattern(r"Customer\s*ID\s*[:.\-\s]+([A-Z0-9/-]+)", text),
        "Total_Amount": amount_value(search_pattern(r"Total\s*Amount\s*Due\s*[:.\-\s]+(?:₹|Rs\.?)?\s*([0-9,.]+)", text)),
        "GSTIN": search_pattern(rf"GSTIN\s*[:.\-\s]+({GSTIN_PATTERN})", text),
    }


def extract_vi(text: str) -> dict:
    return {
        "Vendor": "Vodafone Idea",
        "Invoice_No": search_pattern(r"Invoice\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)", text),
        "Invoice_Date": search_pattern(rf"Invoice\s*Date\s*[:.\-\s]+({DATE_PATTERN})", text),
        "Account_No": search_pattern(r"Account\s*Number\s*[:.\-\s]+([A-Z0-9/-]+)", text),
        "Total_Amount": amount_value(search_pattern(r"Total\s*Amount\s*Payable\s*[:.\-\s]+(?:₹|Rs\.?)?\s*([0-9,.]+)", text)),
        "GSTIN": search_pattern(rf"GSTIN\s*[:.\-\s]+({GSTIN_PATTERN})", text),
    }


def extract_unknown(text: str) -> dict:
    invoice_number = search_pattern(r"Invoice\s*(?:No\.?|Number)\s*[:.\-\s]+([A-Z0-9][A-Z0-9/-]+)", text)
    invoice_date = search_pattern(rf"(?:Invoice|Bill)\s*Date\s*[:.\-\s]+({DATE_PATTERN})", text)
    account_number = search_pattern(r"(?:Account|Relationship|Customer)\s*(?:No\.?|Number|ID)\s*[:.\-\s]+([A-Z0-9][A-Z0-9/-]+)", text)
    amount = search_pattern(r"(?:Total\s*(?:Amount\s*)?(?:Payable|Due)|Amount\s*Due)\s*[:.\-\s]+(?:INR|Rs\.?|₹)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text)
    return {
        "Vendor": "Unknown",
        "Invoice_No": invoice_number,
        "Invoice_Date": invoice_date,
        "Account_No": account_number,
        "Total_Amount": amount_value(amount),
        "GSTIN": search_pattern(rf"GSTIN\s*[:.\-\s]+({GSTIN_PATTERN})", text),
    }


EXTRACTORS = {"Airtel": extract_airtel, "Jio": extract_jio, "Vodafone Idea": extract_vi}


def extract_common_fields(text: str, vendor: str) -> dict:
    """Compatibility wrapper for callers using the original extractor API."""
    return EXTRACTORS.get(vendor, extract_unknown)(text)


def extract_pdf(pdf_path: Path) -> dict:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:5]:
            pages.append(page.extract_text() or "")
    text = normalized_text("\n".join(pages))
    vendor = classify_vendor(text)
    record = extract_common_fields(text, vendor)
    record["Source_File"] = pdf_path.name
    record["Status"] = "Extracted" if text else "No text found - OCR required"
    return record


def process_invoices(pdf_paths: list[Path], reports_dir: Path) -> Path:
    records = []
    for pdf_path in pdf_paths:
        try:
            records.append(extract_pdf(pdf_path))
        except Exception as exc:  # pragma: no cover - protects batch processing
            records.append(
                {
                    "Vendor": "Unknown",
                    "Invoice_No": None,
                    "Invoice_Date": None,
                    "Account_No": None,
                    "Total_Amount": None,
                    "GSTIN": None,
                    "Source_File": pdf_path.name,
                    "Status": f"Extraction failed: {exc}",
                }
            )

    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = reports_dir / f"telecom_invoices_{timestamp}.xlsx"
    columns = ["Vendor", "Invoice_No", "Invoice_Date", "Account_No", "Total_Amount", "GSTIN", "Source_File", "Status"]
    pd.DataFrame(records, columns=columns).to_excel(output_path, index=False)
    return output_path