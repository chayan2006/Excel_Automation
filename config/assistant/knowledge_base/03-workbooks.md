---
title: Workbook automation
url: /#workbooks
---
Workbook automation merges monthly Excel workbooks into one master workbook, Master.xlsx. It is on the Excel control room page.

Master workbook
- Upload a new master with "Upload master". It must be an .xlsx workbook; macro-enabled .xlsm files are accepted as monthly input files only.
- The current master is backed up before it is replaced. If the uploaded file is not a readable workbook, the current master stays unchanged.
- Download the current master with "Download current master".

Processing queue
- Add one or more monthly workbooks (.xlsx or .xlsm) with "Add to queue".
- If a file with the same name is already queued, the queued file is kept and the new upload is skipped.
- One upload can be at most 50 MB in total.

Rules for monthly workbooks
- File name: a word, an underscore and a four-digit year, such as November_2026.xlsx, or a year and month, such as 2026-11.xlsx.
- Worksheet: the sheet named "Monthly Data" is used. If there is none, a sheet named "Sheet1" or "Data" is used, otherwise the first sheet.
- Required columns: Employee ID, Name, Department, Salary and Date. Headers are matched regardless of letter case, spaces and punctuation, so "employee id" and "EmployeeID" both count as Employee ID.
- Employee ID identifies each record, and each Employee ID may appear only once in a file.
- Blank rows are skipped. Required values must be filled in, and dates must be valid dates (the YYYY-MM-DD format is safest).

Check, then apply
- Select "Preview changes" to see, for each queued file, which rows would be added, updated or left unchanged, and which files have errors. A preview changes nothing.
- Applying adds rows with new Employee IDs to the master and updates Department, Salary and Date for Employee IDs that are already there.
- A preview expires after one hour, and has to be repeated if the master, the queue or the processing rules change in the meantime.
- Files with errors are not applied. They leave the queue and their errors are listed in the run's error report.

Run history and errors (/#run-history)
- The ten most recent runs are listed with a summary and an error report to download.
- "Errors & corrections" shows the file, worksheet, row and column of each problem, with a suggested fix. Up to 200 errors are shown on the page; select "Download full error report" for the complete list as a CSV file. Correct the original workbook and upload it again.

Master backups (/#backups)
- Every saved master is listed and can be downloaded.
- To go back to an earlier master, select "Review restore" to check the backup's worksheets and row counts, then confirm the restore. The current master is backed up first.
