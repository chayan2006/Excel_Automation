import json
from datetime import datetime
from pathlib import Path

import pandas as pd


class ReportGenerator:
    def __init__(self, reports_dir: Path):
        self.reports_dir = reports_dir
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def write_error_report(self, error_rows: list[dict], run_id: str) -> Path:
        output_path = self.reports_dir / f"{run_id}_error_report.csv"
        if error_rows:
            pd.DataFrame(error_rows).to_csv(output_path, index=False)
        else:
            pd.DataFrame(columns=["filename", "sheet", "row", "column", "error_type", "error_message"]).to_csv(
                output_path, index=False
            )
        return output_path

    def write_summary_report(self, summary: dict, run_id: str) -> Path:
        summary_path = self.reports_dir / f"{run_id}_summary.json"
        summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary_path
