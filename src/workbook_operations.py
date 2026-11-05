"""Read-only previews, recoverable restores, and dashboard run history."""
import csv
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from openpyxl import load_workbook

from .file_manager import FileManager
from .processor import AutomationRunner

BACKUP_PATTERN = re.compile(r'Master_\d{8}_\d{6}(?:_\d{6})?\.xlsx')
RUN_PATTERN = re.compile(r'\d{8}_\d{6}(?:_\d{6})?')
ERROR_HELP = {
    'invalid_filename': 'Rename the workbook to match your configured naming rule, then upload it again (for example, November_2026.xlsx).',
    'missing_required_column': 'Add this column with its configured header, then upload the corrected workbook.',
    'duplicate_column': 'Give each column a unique header. Check whether two aliases map to the same name.',
    'empty_sheet': 'Add data below the header row in the selected worksheet.',
    'empty_row': 'Remove the blank row or enable skip_blank_rows in the configuration.',
    'missing_value': 'Fill in the required value at this row and column, then upload the corrected workbook.',
    'duplicate_record': 'Remove or correct duplicate identifier combinations in the input file.',
    'invalid_date': 'Use a valid date, preferably YYYY-MM-DD, in this cell.',
    'read_error': 'Open the file in Excel, check that it is readable and not password protected, then save a new .xlsx copy.',
    'update_error': 'Check the master headers and duplicate IDs. Close the workbook in Excel and ensure the folder is writable.',
}


def explain_errors(errors):
    return [{**error, 'suggestion': ERROR_HELP.get(error.get('error_type'), 'Correct the reported problem and upload the workbook again.')} for error in errors]


def file_digest(path: Path) -> str:
    if not path.exists():
        return 'missing'
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def queue_fingerprint(root: Path, config_path: Path) -> str:
    files = [('Master.xlsx', root / 'Master.xlsx'), ('config.json', config_path)]
    files += [(f'input/{path.name}', path) for path in FileManager(root).list_input_files()]
    return hashlib.sha256(json.dumps([(name, file_digest(path)) for name, path in files]).encode()).hexdigest()


def preview_queue(root: Path, config_path: Path) -> dict:
    """Run the exact processing rules on disposable copies, in the real queue order."""
    manager = FileManager(root)
    paths = manager.list_input_files()
    if not (root / 'Master.xlsx').is_file() or not paths:
        raise ValueError('Upload a master and at least one queued workbook before previewing.')
    fingerprint = queue_fingerprint(root, config_path)
    files = []
    totals = dict(processed_count=0, failed_count=0, inserted_count=0, updated_count=0, skipped_count=0)
    with TemporaryDirectory(prefix='excel-preview-') as temporary:
        scratch = Path(temporary)
        scratch_manager = FileManager(scratch)
        scratch_manager.ensure_structure()
        shutil.copy2(root / 'Master.xlsx', scratch / 'Master.xlsx')
        shutil.copy2(config_path, scratch / 'config.json')
        for path in paths:
            shutil.copy2(path, scratch_manager.input_dir / path.name)
        if queue_fingerprint(scratch, scratch / 'config.json') != fingerprint:
            raise ValueError('Files changed while preparing the preview. Generate a fresh preview.')
        logger = logging.Logger('excel-preview')
        logger.addHandler(logging.NullHandler())
        runner = AutomationRunner(scratch, scratch / 'config.json', logger=logger)
        for path in scratch_manager.list_input_files():
            details = []
            outcome = runner.process_file(path, change_details=details)
            outcome['errors'] = explain_errors(outcome['errors'])
            if outcome['status'] == 'processed':
                totals['processed_count'] += 1
                for key in ('inserted_count', 'updated_count', 'skipped_count'):
                    totals[key] += outcome['result'][key]
                outcome['changes'] = details
                outcome['omitted'] = outcome['result']['inserted_count'] + outcome['result']['updated_count'] - len(details)
            else:
                totals['failed_count'] += 1
                outcome['changes'] = []
            files.append(outcome)
    if queue_fingerprint(root, config_path) != fingerprint:
        raise ValueError('The queue or master changed during preview. Generate a fresh preview.')
    return {'fingerprint': fingerprint, 'files': files, 'totals': totals}


def backup_path(root: Path, name: str) -> Path:
    directory = (root / 'backups').resolve()
    path = directory / name
    if not BACKUP_PATTERN.fullmatch(name) or path.is_symlink() or path.resolve().parent != directory or not path.is_file():
        raise ValueError('That backup is no longer available.')
    return path


def list_backups(root: Path) -> list[dict]:
    directory = root / 'backups'
    if not directory.exists():
        return []
    backups = []
    for path in sorted(directory.iterdir(), reverse=True):
        if BACKUP_PATTERN.fullmatch(path.name) and path.is_file() and not path.is_symlink():
            stamp = path.stem.removeprefix('Master_')
            try:
                created = datetime.strptime(stamp, '%Y%m%d_%H%M%S_%f' if len(stamp) > 15 else '%Y%m%d_%H%M%S')
            except ValueError:
                continue
            backups.append({'name': path.name, 'created': created.strftime('%d %b %Y, %H:%M:%S'), 'size': path.stat().st_size})
    return backups


def restore_fingerprint(root: Path, selected: Path) -> str:
    return hashlib.sha256(f'{file_digest(root / "Master.xlsx")}:{file_digest(selected)}'.encode()).hexdigest()


def workbook_overview(path: Path) -> list[dict]:
    # Parse the complete sheets; lazy read-only loading can miss corrupted cells
    # after the dimensions header and incorrectly approve an unreadable backup.
    workbook = load_workbook(path, read_only=False)
    try:
        return [{'name': sheet.title, 'rows': max(0, (sheet.max_row or 1) - 1)} for sheet in workbook]
    finally:
        workbook.close()


def restore_backup(root: Path, name: str, expected_fingerprint: str) -> Path | None:
    selected = backup_path(root, name)
    if restore_fingerprint(root, selected) != expected_fingerprint:
        raise ValueError('The master or backup changed. Review the restore again before confirming.')
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=root, suffix='.xlsx', delete=False) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copy2(selected, temporary_path)
        workbook_overview(temporary_path)  # Reject unreadable backups before touching the live workbook.
        if file_digest(temporary_path) != file_digest(selected) or restore_fingerprint(root, selected) != expected_fingerprint:
            raise ValueError('Files changed during restore. Review the restore again.')
        manager = FileManager(root)
        current = root / 'Master.xlsx'
        safety_copy = manager.backup_master(current) if current.exists() else None
        os.replace(temporary_path, current)
        return safety_copy
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_history(root: Path, limit: int = 10) -> list[dict]:
    reports = root / 'reports'
    result = []
    for path in sorted(reports.glob('*_summary.json'), reverse=True):
        run_id = path.name.removesuffix('_summary.json')
        if not RUN_PATTERN.fullmatch(run_id):
            continue
        try:
            summary = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(summary, dict):
                continue
            summary['run_id'] = run_id
            summary['has_errors'] = (reports / f'{run_id}_error_report.csv').is_file()
            result.append(summary)
        except (OSError, ValueError):
            continue
        if len(result) >= limit:
            break
    return result


def run_errors(root: Path, run_id: str, limit: int = 200) -> dict:
    if not RUN_PATTERN.fullmatch(run_id):
        raise ValueError('Invalid run identifier.')
    path = root / 'reports' / f'{run_id}_error_report.csv'
    if not path.is_file():
        return {'rows': [], 'count': 0, 'available': False}
    rows, count = [], 0
    with path.open(newline='', encoding='utf-8-sig') as source:
        for error in csv.DictReader(source):
            count += 1
            if len(rows) < limit:
                # Existing reports sometimes serialize Excel row numbers as "2.0".
                row = error.get('row', '')
                if row and row.endswith('.0'):
                    error['row'] = row[:-2]
                rows.append(error)
    return {'rows': explain_errors(rows), 'count': count, 'available': True}
