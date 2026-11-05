"""Regression tests use temporary directories; never touch live workbooks or review data."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import Workbook, load_workbook

import app as web
from review import review_value
from src.excel_reader import ExcelReader
from src.file_manager import FileManager
from src.master_updater import MasterUpdater
from src.validator import validate_dataframe

CONFIG = {
    'sheet_name': 'Monthly Data',
    'required_columns': ['Employee ID', 'Name'],
    'identifier_columns': ['Employee ID'],
    'business_rules': {'update_columns': ['Name'], 'skip_blank_rows': True},
}


def workbook_bytes(rows, title='Monthly Data'):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class ValidationTests(unittest.TestCase):
    def validate(self, rows, config=CONFIG):
        return validate_dataframe(pd.DataFrame(rows), config, 'November_2026.xlsx', 'Monthly Data')

    def test_blank_identifier_reports_error_without_crashing(self):
        _, errors = self.validate({'Employee ID': ['1', ''], 'Name': ['Alice', 'Bob']})
        self.assertIn('missing_value', [error['error_type'] for error in errors])

    def test_composite_identifiers_allow_shared_components(self):
        config = {**CONFIG, 'identifier_columns': ['Employee ID', 'Name']}
        _, errors = self.validate({'Employee ID': ['1', '1'], 'Name': ['Alice', 'Bob']}, config)
        self.assertEqual(errors, [])

    def test_composite_duplicates_are_rejected(self):
        config = {**CONFIG, 'identifier_columns': ['Employee ID', 'Name']}
        _, errors = self.validate({'Employee ID': ['1', '1'], 'Name': ['Alice', 'Alice']}, config)
        self.assertEqual(errors[0]['error_type'], 'duplicate_record')

    def test_blank_rows_are_skipped_without_losing_excel_row_numbers(self):
        cleaned, errors = self.validate({'Employee ID': ['1', None, '2'], 'Name': ['Alice', None, '']})
        self.assertEqual(cleaned.index.tolist(), [0, 2])
        self.assertEqual(errors[0]['row'], 4)

    def test_wholly_blank_sheet_is_rejected(self):
        _, errors = self.validate({'Employee ID': [None], 'Name': [None]})
        self.assertIn('empty_sheet', [error['error_type'] for error in errors])

    def test_duplicate_headers_do_not_crash(self):
        df = pd.DataFrame([['1', '2']], columns=['Employee ID', ' Employee ID '])
        _, errors = validate_dataframe(df, CONFIG, 'test.xlsx', 'Monthly Data')
        self.assertEqual(errors[0]['error_type'], 'duplicate_column')

    def test_invalid_dates_report_missing_and_invalid(self):
        config = {**CONFIG, 'required_columns': ['Employee ID', 'Name', 'Date']}
        _, errors = self.validate({'Employee ID': ['1'], 'Name': ['Alice'], 'Date': ['nonsense']}, config)
        self.assertEqual({e['error_type'] for e in errors}, {'missing_value', 'invalid_date'})


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'Master.xlsx'
        self.updater = MasterUpdater(CONFIG)

    def write(self, rows, title='Monthly Data'):
        self.path.write_bytes(workbook_bytes(rows, title))

    def rows(self):
        workbook = load_workbook(self.path)
        try:
            return list(workbook['Monthly Data'].values)
        finally:
            workbook.close()

    def update(self, rows):
        return self.updater.update_master(self.path, pd.DataFrame(rows), 'November_2026.xlsx')

    def test_updates_correct_row_after_blank_row(self):
        self.write([['Employee ID', 'Name'], ['1', 'Alice'], [None, None], ['2', 'Bob']])
        result = self.update({'Employee ID': ['2'], 'Name': ['Updated Bob']})
        self.assertEqual(result['updated_count'], 1)
        self.assertEqual(self.rows()[2], (None, None))
        self.assertEqual(self.rows()[3], ('2', 'Updated Bob'))

    def test_new_sheet_starts_in_column_a(self):
        self.write([['Unrelated'], ['Keep me']], 'Other')
        self.update({'Employee ID': ['1'], 'Name': ['Alice']})
        self.assertEqual(self.rows(), [('Employee ID', 'Name'), ('1', 'Alice')])
        workbook = load_workbook(self.path)
        self.assertEqual(workbook['Other']['A2'].value, 'Keep me')
        workbook.close()

    def test_trimmed_headers_update_existing_columns(self):
        self.write([[' Employee ID ', ' Name '], ['1', 'Alice']])
        self.update({'Employee ID': ['1'], 'Name': ['Updated']})
        self.assertEqual(self.rows()[1], ('1', 'Updated'))
        self.assertEqual(len(self.rows()[0]), 2)

    def test_inserted_identifier_is_reused(self):
        self.write([['Employee ID', 'Name']])
        result = self.update({'Employee ID': ['1', '1'], 'Name': ['Alice', 'Updated']})
        self.assertEqual(result['inserted_count'], 1)
        self.assertEqual(self.rows(), [('Employee ID', 'Name'), ('1', 'Updated')])

    def test_duplicate_master_ids_do_not_modify_file(self):
        self.write([['Employee ID', 'Name'], ['1', 'Alice'], ['1', 'Bob']])
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Duplicate identifier'):
            self.update({'Employee ID': ['1'], 'Name': ['Updated']})
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_save_preserves_master(self):
        self.write([['Employee ID', 'Name'], ['1', 'Alice']])
        original = self.path.read_bytes()
        with patch('src.master_updater.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.update({'Employee ID': ['1'], 'Name': ['Updated']})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_leading_zero_ids_are_preserved(self):
        self.write([['Employee ID', 'Name'], ['0012', 'Alice'], [None, 'Bob']])
        data, _ = ExcelReader(CONFIG).read_excel(self.path)
        self.assertEqual(data.iloc[0]['Employee ID'], '0012')

    def test_rapid_backups_are_distinct(self):
        self.write([['Employee ID', 'Name']])
        manager = FileManager(self.root)
        paths = [manager.backup_master(self.path) for _ in range(3)]
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(path.is_file() for path in paths))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(web, 'PROJECT_ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = web.app.test_client()

    def upload(self, route, field, content, name):
        return self.client.post(route, data={field: (io.BytesIO(content), name)}, follow_redirects=True)

    def test_invalid_master_upload_preserves_existing_master(self):
        original = workbook_bytes([['Employee ID', 'Name'], ['1', 'Alice']])
        (self.root / 'Master.xlsx').write_bytes(original)
        with self.assertLogs(web.app.logger, level='ERROR'):
            response = self.upload('/upload-master', 'master_file', b'not an excel file', 'fake.xlsx')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'current master is unchanged', response.data)
        self.assertEqual((self.root / 'Master.xlsx').read_bytes(), original)

    def test_valid_master_upload_creates_backup(self):
        original = workbook_bytes([['Employee ID', 'Name']])
        replacement = workbook_bytes([['Employee ID', 'Name'], ['1', 'Alice']])
        (self.root / 'Master.xlsx').write_bytes(original)
        response = self.upload('/upload-master', 'master_file', replacement, 'new.xlsx')
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.root / 'Master.xlsx').read_bytes(), replacement)
        self.assertEqual(next((self.root / 'backups').iterdir()).read_bytes(), original)

    def test_duplicate_uploads_keep_original(self):
        for route, field, name, directory in [
            ('/upload-files', 'data_files', 'November_2026.xlsx', 'input'),
            ('/upload-invoices', 'invoice_files', 'invoice.pdf', 'input/invoices'),
        ]:
            self.upload(route, field, b'original', name)
            response = self.upload(route, field, b'replacement', name)
            self.assertIn(b'already queued', response.data)
            self.assertNotIn(b'Choose at least one', response.data)
            self.assertEqual((self.root / directory / name).read_bytes(), b'original')

    def test_uppercase_pdf_extension_is_processed(self):
        self.upload('/upload-invoices', 'invoice_files', b'pdf placeholder', 'INVOICE.PDF')
        with patch.object(web, 'process_invoices', return_value=self.root / 'reports/result.xlsx') as process:
            response = self.client.post('/extract-invoices', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(process.call_args.args[0][0].name, 'INVOICE.PDF')
        self.assertTrue((self.root / 'processed/invoices/INVOICE.PDF').is_file())

    def test_numeric_zero_is_displayed(self):
        self.assertEqual(review_value(0, {'type': 'number'}), '0.00')


if __name__ == '__main__':
    unittest.main()
