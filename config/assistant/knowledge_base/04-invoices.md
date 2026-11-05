---
title: Telecom invoice extraction
url: /#invoices
---
Telecom invoice extraction reads invoice PDFs and saves their key values to an Excel report. It is in the "Telecom invoice extraction" panel of the Excel control room.

- Upload one or more invoice PDFs with "Add invoices". Airtel, Jio and Vodafone Idea invoices are recognised automatically; invoices from other providers are read with general rules.
- Select "Extract to Excel" to read every waiting PDF. The report has one row per PDF with the file name, detected vendor, invoice number, invoice date, account number, total amount, GSTIN and extraction status.
- Download the newest report with "Download latest invoice report".
- After extraction the PDFs are archived and leave the waiting list.
- The PDFs must contain real text. Scanned (image-only) PDFs need OCR first; without it no values can be extracted from them.
- To check every value against the PDF before it reaches Excel, use the invoice review workspace instead.
