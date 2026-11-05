import json
from datetime import datetime
from pathlib import Path

try:
    from .excel_reader import ExcelReader
    from .file_manager import FileManager
    from .logger_config import configure_logger
    from .master_updater import MasterUpdater
    from .report_generator import ReportGenerator
    from .validator import validate_dataframe, validate_filename
except ImportError:  # pragma: no cover
    from excel_reader import ExcelReader
    from file_manager import FileManager
    from logger_config import configure_logger
    from master_updater import MasterUpdater
    from report_generator import ReportGenerator
    from validator import validate_dataframe, validate_filename


class AutomationRunner:
    def __init__(self, project_root: Path, config_path: Path, logger=None):
        self.project_root = project_root
        self.file_manager = FileManager(project_root)
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.logger = logger if logger is not None else configure_logger(self.file_manager.logs_dir)
        self.report_generator = ReportGenerator(self.file_manager.reports_dir)
        self.master_updater = MasterUpdater(self.config)
        self.excel_reader = ExcelReader(self.config)

    def process_file(self, file_path: Path, change_details: list | None = None):
        file_name = file_path.name
        logger = self.logger
        logger.info("Processing file: %s", file_name)

        filename_error = validate_filename(file_name, self.config.get("filename_pattern"))
        if filename_error:
            logger.error(filename_error)
            return {
                "status": "failed",
                "file_name": file_name,
                "errors": [
                    {
                        "filename": file_name,
                        "sheet": None,
                        "row": None,
                        "column": None,
                        "error_type": "invalid_filename",
                        "error_message": filename_error,
                    }
                ],
            }

        try:
            dataframe, sheet_name = self.excel_reader.read_excel(file_path)
        except Exception as exc:  # pragma: no cover - runtime safety
            logger.exception("Unable to read file: %s", file_name)
            return {
                "status": "failed",
                "file_name": file_name,
                "errors": [
                    {
                        "filename": file_name,
                        "sheet": None,
                        "row": None,
                        "column": None,
                        "error_type": "read_error",
                        "error_message": str(exc),
                    }
                ],
            }

        cleaned_df, validation_errors = validate_dataframe(dataframe, self.config, file_name, sheet_name)
        if validation_errors:
            logger.error("Validation failed for %s: %s", file_name, validation_errors)
            return {
                "status": "failed",
                "file_name": file_name,
                "errors": validation_errors,
            }

        master_path = self.project_root / "Master.xlsx"
        if not master_path.exists():
            logger.warning("Master workbook not found at %s. Creating a new one.", master_path)
            self.master_updater.ensure_master_workbook(master_path, sheet_name)

        try:
            self.file_manager.backup_master(master_path)
            result = self.master_updater.update_master(master_path, cleaned_df, file_name, change_details=change_details)
            logger.info("Updated master for %s: %s", file_name, result)
            return {
                "status": "processed",
                "file_name": file_name,
                "result": result,
                "errors": [],
            }
        except Exception as exc:  # pragma: no cover - runtime safety
            logger.exception("Failed to update master for %s", file_name)
            return {
                "status": "failed",
                "file_name": file_name,
                "errors": [
                    {
                        "filename": file_name,
                        "sheet": sheet_name,
                        "row": None,
                        "column": None,
                        "error_type": "update_error",
                        "error_message": str(exc),
                    }
                ],
            }

    def run(self):
        self.file_manager.ensure_structure()
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.logger.info("Starting automation run %s", run_id)

        all_files = self.file_manager.list_input_files()
        processed_count = 0
        failed_count = 0
        inserted_count = 0
        updated_count = 0
        skipped_count = 0
        all_errors = []

        for file_path in all_files:
            outcome = self.process_file(file_path)
            if outcome["status"] == "processed":
                processed_count += 1
                inserted_count += outcome["result"].get("inserted_count", 0)
                updated_count += outcome["result"].get("updated_count", 0)
                skipped_count += outcome["result"].get("skipped_count", 0)
                self.file_manager.move_file(file_path, "processed")
            else:
                failed_count += 1
                all_errors.extend(outcome["errors"])
                self.file_manager.move_file(file_path, "errors")

        summary = {
            "run_id": run_id,
            "files_total": len(all_files),
            "processed_count": processed_count,
            "failed_count": failed_count,
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
        }

        error_report_path = self.report_generator.write_error_report(all_errors, run_id)
        summary_report_path = self.report_generator.write_summary_report(summary, run_id)

        self.logger.info("Run summary: %s", summary)
        self.logger.info("Error report: %s", error_report_path)
        self.logger.info("Summary report: %s", summary_report_path)
        return summary
