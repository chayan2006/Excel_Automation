---
title: Invoice review workspace
url: /review/documents
---
In the review workspace a person checks every value extracted from an invoice against the PDF before it is exported to Excel. Open it with "Review workspace" on the Excel control room.

How it works
1. On the Documents page, upload PDF invoices into a queue with "Upload documents", or drag and drop them onto the page. The data is extracted in the background.
2. Open a document from the "To review" tab, or select "Start reviewing". Values the extractor is unsure about are marked "Check".
3. Correct a value by typing, or select a field and draw a box around the value on the page to capture it. Enter accepts a value and moves to the next one to check.
4. Confirm the document (Ctrl+Enter). Errors, such as an empty required field or an invalid GSTIN, block confirmation; warnings, such as totals that do not add up, do not.
5. Export confirmed documents to Excel from the review screen, or several at once from the "Confirmed" tab. The export has one row per document plus a sheet per table (tax breakdown and line items).

Statuses: Importing, Import failed, To review, Postponed, Confirmed, Exported, Rejected and Deleted. Postponed documents can still be edited. Confirmed, exported and rejected documents are read-only until someone selects "Return to review", and deleted documents can be restored.

Queues: documents are organised in queues, listed in the left sidebar of the Documents page.

Review screen
- The fields sidebar groups the values into sections, with tables for the tax breakdown and line items; table rows can be added and deleted.
- The document viewer draws a box around every value. Selecting a field highlights it on the page, and clicking a box selects its field. The viewer has zoom, page thumbnails and a page jump.
- The "Confidence" switch shows extraction scores, and "Hidden fields" shows fields that are normally hidden.
- Also available: an activity log of every change, document information, labels, moving documents between queues, extracting the data again, rejecting or postponing with a note, and downloading the original PDF.
- Keyboard shortcuts: Enter (accept and go to the next value), Tab, Ctrl+Enter (confirm), N (next value to check), [ and ] (previous and next document), + and - (zoom), 0 (reset zoom), ? (list of shortcuts).

Limits
- Scanned (image-only) PDFs can be viewed, but nothing can be extracted or captured from them without OCR.
- Documents arrive by upload only; there is no email import.
- Pages cannot be split, rotated or deleted.
- Documents are not locked while someone works on them, so agree who works on which queue.
