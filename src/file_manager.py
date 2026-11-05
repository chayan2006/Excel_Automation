import json
import shutil
from datetime import datetime
from pathlib import Path


class FileManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.input_dir = project_root / "input"
        self.processed_dir = project_root / "processed"
        self.errors_dir = project_root / "errors"
        self.output_dir = project_root / "output"
        self.backups_dir = project_root / "backups"
        self.reports_dir = project_root / "reports"
        self.logs_dir = project_root / "logs"

    def ensure_structure(self) -> None:
        for directory in [
            self.input_dir,
            self.processed_dir,
            self.errors_dir,
            self.output_dir,
            self.backups_dir,
            self.reports_dir,
            self.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def list_input_files(self):
        allowed = {".xlsx", ".xlsm"}
        return sorted(
            path for path in self.input_dir.iterdir() if path.is_file() and path.suffix.lower() in allowed
        )

    def move_file(self, source_path: Path, destination_folder: str, suffix: str | None = None) -> Path:
        destination_dir = getattr(self, f"{destination_folder}_dir")
        destination_dir.mkdir(parents=True, exist_ok=True)
        if suffix:
            destination_path = destination_dir / f"{source_path.stem}_{suffix}{source_path.suffix}"
        else:
            destination_path = destination_dir / source_path.name

        if destination_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            destination_path = destination_dir / f"{source_path.stem}_{timestamp}{source_path.suffix}"

        shutil.move(str(source_path), str(destination_path))
        return destination_path

    def backup_master(self, master_path: Path) -> Path:
        if not master_path.exists():
            raise FileNotFoundError(f"Master workbook does not exist: {master_path}")

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = self.backups_dir / f"{master_path.stem}_{timestamp}{master_path.suffix}"
        shutil.copy2(master_path, backup_path)
        return backup_path

    @staticmethod
    def load_json(config_path: Path):
        with config_path.open("r", encoding="utf-8") as file:
            return json.load(file)
