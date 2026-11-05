"""Verify preview/apply/restore against real temporary Excel files and Flask routes."""
import html
import io
import json
import logging
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app as web
from src.file_manager import FileManager
from src.workbook_operations import (
    backup_path, file_digest, list_backups, preview_queue,
    restore_backup, restore_fingerprint, run_errors, run_history,
)
from test_regressions import CONFIG, workbook_bytes


class WorkbookOperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        FileManager(self.root).ensure_structure()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(CONFIG))
        self.master = self.root / 'Master.xlsx'
        self.master.write_bytes(workbook_bytes([['Employee ID', 'Name'], ['1', 'Alice']]))
        for name, rows in [
            ('01.xlsx', [['1', 'Updated Alice'], ['2', 'Bob']]),
            ('02.xlsx', [['2', 'Updated Bob']]),
            ('03.xlsx', [[None, 'Missing ID']]),
        ]:
            (self.root / 'input' / name).write_bytes(workbook_bytes([['Employee ID', 'Name'], *rows]))
        for attribute, value in [('PROJECT_ROOT', self.root), ('CONFIG_PATH', self.config_path)]:
            patcher = patch.object(web, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        logger = logging.Logger('test-operations')
        logger.addHandler(logging.NullHandler())
        patcher = patch('src.processor.configure_logger', return_value=logger)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = web.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.csrf = session['review_csrf']

    def snapshot(self):
        return {str(path.relative_to(self.root)): file_digest(path)
                for path in self.root.rglob('*') if path.is_file()}

    def hidden(self, response, name):
        match = re.search(r'name="' + name + r'" value="([^"]*)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, response.get_data(as_text=True)[:1000])
        return html.unescape(match.group(1))

    def get_preview(self):
        response = self.client.post('/preview', data={'csrf': self.csrf})
        self.assertEqual(response.status_code, 200)
        return response

    def apply(self, token):
        return self.client.post('/run', data={'csrf': self.csrf, 'preview_token': token}, follow_redirects=True)

    def backup(self):
        path = self.root / 'backups/Master_20261101_120000.xlsx'
        path.write_bytes(workbook_bytes([['Employee ID', 'Name'], ['1', 'Earlier Alice']]))
        return path

    def test_preview_is_read_only_and_models_sequential_changes(self):
        original = self.snapshot()
        preview = preview_queue(self.root, self.config_path)
        self.assertEqual(original, self.snapshot())
        self.assertEqual(preview['totals'], {
            'processed_count': 2, 'failed_count': 1, 'inserted_count': 1,
            'updated_count': 2, 'skipped_count': 0,
        })
        self.assertEqual(preview['files'][1]['changes'][0]['values'][0],
                         {'column': 'Name', 'before': 'Bob', 'after': 'Updated Bob'})
        self.assertEqual(preview['files'][2]['errors'][0]['row'], 2)
        self.assertIn('Fill in', preview['files'][2]['errors'][0]['suggestion'])

    def test_preview_limits_details_without_losing_totals(self):
        (self.root / 'input/04.xlsx').write_bytes(workbook_bytes([
            ['Employee ID', 'Name'], *[[str(i), f'Employee {i}'] for i in range(10, 215)]
        ]))
        result = preview_queue(self.root, self.config_path)['files'][-1]
        self.assertEqual(result['result']['inserted_count'], 205)
        self.assertEqual(len(result['changes']), 200)
        self.assertEqual(result['omitted'], 5)

    def test_preview_page_apply_and_download_reports(self):
        original = self.snapshot()
        preview = self.get_preview()
        self.assertIn(b'Updated Bob', preview.data)
        self.assertIn(b'Needs correction', preview.data)
        self.assertEqual(original, self.snapshot())
        token = self.hidden(preview, 'preview_token')
        response = self.apply(token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'2 processed, 1 failed', response.data)
        self.assertIn(b'Missing required value', response.data)
        self.assertIn(b'Fill in the required value', response.data)
        self.assertEqual(len(list((self.root / 'processed').glob('*.xlsx'))), 2)
        self.assertTrue((self.root / 'errors/03.xlsx').exists())
        self.assertEqual(len(list_backups(self.root)), 2)
        run = run_history(self.root)[0]
        self.assertEqual((run['inserted_count'], run['updated_count']), (1, 2))
        report = self.client.get(f'/reports/{run["run_id"]}/errors')
        self.assertEqual(report.status_code, 200)
        self.assertIn(b'03.xlsx', report.data)
        report.close()
        done = self.snapshot()
        self.apply(token)
        self.assertEqual(self.snapshot(), done)

    def test_changed_inputs_invalidate_preview(self):
        for target in ['master', 'queue', 'config']:
            with self.subTest(target=target):
                token = self.hidden(self.get_preview(), 'preview_token')
                if target == 'master':
                    self.master.write_bytes(workbook_bytes([['Employee ID', 'Name'], ['1', 'Changed']]))
                elif target == 'queue':
                    (self.root / 'input/new.xlsx').write_bytes(workbook_bytes([['Employee ID', 'Name'], ['3', 'New']]))
                else:
                    self.config_path.write_text(json.dumps({**CONFIG, 'filename_pattern': 'changed'}))
                before = self.snapshot()
                response = self.apply(token)
                self.assertIn(b'Preview the changes again', response.data)
                self.assertEqual(self.snapshot(), before)

    def test_run_requires_valid_preview_and_csrf(self):
        original = self.snapshot()
        response = self.apply('forged-token')
        self.assertIn(b'expired or is invalid', response.data)
        token = self.hidden(self.get_preview(), 'preview_token')
        response = self.client.post('/run', data={'preview_token': token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), original)

    def test_expired_preview_does_not_run(self):
        fingerprint = preview_queue(self.root, self.config_path)['fingerprint']
        # Backdate only the confirmation token, leaving Flask's session cookie valid.
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=100000):
            token = web.confirmation_tokens('preview').dumps({'fingerprint': fingerprint})
        original = self.snapshot()
        self.assertIn(b'expired or is invalid', self.apply(token).data)
        self.assertEqual(self.snapshot(), original)

    def test_restore_confirmation_preserves_previous_master_and_queue(self):
        selected = self.backup()
        previous = self.master.read_bytes()
        original = self.snapshot()
        response = self.client.get(f'/backups/{selected.name}/restore')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Monthly Data', response.data)
        self.assertEqual(original, self.snapshot())
        response = self.client.post(f'/backups/{selected.name}/restore', data={
            'csrf': self.csrf, 'restore_token': self.hidden(response, 'restore_token'),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Previous master saved', response.data)
        self.assertEqual(self.master.read_bytes(), selected.read_bytes())
        copies = [path for path in (self.root / 'backups').glob('*.xlsx') if path != selected]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), previous)
        for path, digest in original.items():
            if path != 'Master.xlsx':
                self.assertEqual(file_digest(self.root / path), digest)

    def test_stale_restore_token_cannot_replace_new_master(self):
        selected = self.backup()
        response = self.client.get(f'/backups/{selected.name}/restore')
        token = self.hidden(response, 'restore_token')
        self.master.write_bytes(workbook_bytes([['Employee ID', 'Name'], ['1', 'Newer master']]))
        original = self.snapshot()
        response = self.client.post(f'/backups/{selected.name}/restore', data={
            'csrf': self.csrf, 'restore_token': token,
        }, follow_redirects=True)
        self.assertIn(b'master or backup changed', response.data)
        self.assertEqual(self.snapshot(), original)

    def test_restore_failed_replace_leaves_master_intact(self):
        selected = self.backup()
        original = self.master.read_bytes()
        with patch('src.workbook_operations.os.replace', side_effect=OSError('file locked')):
            with self.assertRaises(OSError):
                restore_backup(self.root, selected.name, restore_fingerprint(self.root, selected))
        self.assertEqual(self.master.read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.xlsx')), [self.master])

    def test_corrupt_backup_is_rejected_before_replacement(self):
        selected = self.backup()
        selected.write_bytes(b'corrupt workbook')
        original = self.snapshot()
        response = self.client.get(f'/backups/{selected.name}/restore', follow_redirects=True)
        self.assertIn(b'not a readable Excel workbook', response.data)
        self.assertEqual(self.snapshot(), original)

    def test_restore_can_recover_missing_master(self):
        selected = self.backup()
        self.master.unlink()
        copy = restore_backup(self.root, selected.name, restore_fingerprint(self.root, selected))
        self.assertIsNone(copy)
        self.assertEqual(self.master.read_bytes(), selected.read_bytes())

    def test_backup_with_corrupt_sheet_xml_cannot_replace_master(self):
        selected = self.backup()
        damaged = io.BytesIO()
        with zipfile.ZipFile(selected) as source, zipfile.ZipFile(damaged, 'w') as target:
            for name in source.namelist():
                content = source.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    content = content.replace(b'</sheetData>', b'</brokenSheetData>')
                target.writestr(name, content)
        selected.write_bytes(damaged.getvalue())
        original = self.snapshot()
        with self.assertRaises(Exception):
            restore_backup(self.root, selected.name, restore_fingerprint(self.root, selected))
        self.assertEqual(self.snapshot(), original)

    def test_backup_paths_are_constrained_and_malformed_names_do_not_crash_dashboard(self):
        for name in ['../Master.xlsx', '/tmp/Master.xlsx', 'Master.xlsx']:
            with self.assertRaises(ValueError):
                backup_path(self.root, name)
        (self.root / 'backups/Master_20269999_999999.xlsx').write_bytes(b'invalid filename')
        (self.root / 'backups/Master_20261101_130000.xlsx').symlink_to(self.master)
        self.assertEqual(list_backups(self.root), [])
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/reports/not-a-run/errors').status_code, 404)

    def test_error_panel_escapes_content_and_handles_large_reports(self):
        run_id = '20261104_120000'
        (self.root / 'reports' / f'{run_id}_error_report.csv').write_text(
            'filename,sheet,row,column,error_type,error_message\n' +
            'bad.xlsx,Monthly Data,2.0,Name,missing_value,<script>alert(1)</script>\n' * 205
        )
        errors = run_errors(self.root, run_id)
        self.assertEqual(errors['count'], 205)
        self.assertEqual(len(errors['rows']), 200)
        self.assertEqual(errors['rows'][0]['row'], '2')
        response = self.client.get(f'/?run={run_id}')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertIn(b'first 200 of 205', response.data)

    def test_broken_latest_summary_does_not_hide_valid_history(self):
        reports = self.root / 'reports'
        summary = dict(run_id='20261101_120000', processed_count=2, failed_count=0,
                       inserted_count=1, updated_count=1, skipped_count=0)
        (reports / '20261101_120000_summary.json').write_text(json.dumps(summary))
        (reports / '20261104_120000_summary.json').write_text('{broken')
        self.assertEqual(run_history(self.root)[0]['run_id'], '20261101_120000')
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_healthz_endpoint(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'ok')
        self.assertIn('timestamp', payload)

    def test_sync_master_to_data_copies_when_data_dir_present(self):
        data_dir = self.root / 'data'
        data_dir.mkdir()
        self.assertFalse((data_dir / 'Master.xlsx').exists())
        web.sync_master_to_data(self.master)
        self.assertTrue((data_dir / 'Master.xlsx').exists())
        self.assertEqual((data_dir / 'Master.xlsx').read_bytes(), self.master.read_bytes())


if __name__ == '__main__':
    unittest.main()
