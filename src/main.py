from pathlib import Path

from processor import AutomationRunner


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config" / "config.json"
    runner = AutomationRunner(project_root, config_path)
    summary = runner.run()
    print(f"Automation finished. Summary: {summary}")


if __name__ == "__main__":
    main()
