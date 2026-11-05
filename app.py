import os
import secrets
import shutil
import threading
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from tempfile import NamedTemporaryFile

import click
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from flask.cli import AppGroup
from itsdangerous import BadSignature, URLSafeTimedSerializer
from openpyxl import load_workbook
from werkzeug.utils import secure_filename

from assistant import assistant_bp
from review import check_csrf, review_bp
from src.auth import UserStore
from src.file_manager import FileManager
from src.invoice_extractor import process_invoices
from src.processor import AutomationRunner
from src.workbook_operations import (
    RUN_PATTERN, backup_path, list_backups, preview_queue, queue_fingerprint,
    restore_backup, restore_fingerprint, run_errors, run_history, workbook_overview,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
ALLOWED_EXTENSIONS = {".xlsx", ".xlsm"}
INVOICE_EXTENSION = ".pdf"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("EXCEL_AUTOMATION_SECRET") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
# A sign-in lasts until 12 hours without activity; Lax cookies are not sent with form posts from other websites.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.register_blueprint(review_bp)
app.register_blueprint(assistant_bp)
_workbook_lock = threading.RLock()
PUBLIC_ENDPOINTS = {"login", "healthz", "static"}


def serialized_workbooks(view):
    """Serialize local dashboard writes so a run cannot race an upload or restore."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        with _workbook_lock:
            return view(*args, **kwargs)
    return wrapper


def confirmation_tokens(purpose):
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=f"workbooks-{purpose}")


def file_manager() -> FileManager:
    manager = FileManager(PROJECT_ROOT)
    manager.ensure_structure()
    return manager


def is_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def is_invoice(filename: str) -> bool:
    return Path(filename).suffix.lower() == INVOICE_EXTENSION


def sync_master_to_data(master_path: Path | None = None) -> None:
    data_dir = PROJECT_ROOT / "data"
    target = master_path or (PROJECT_ROOT / "Master.xlsx")
    if data_dir.is_dir() and target.is_file():
        try:
            shutil.copy2(target, data_dir / "Master.xlsx")
        except Exception:
            app.logger.warning("Could not sync Master.xlsx to data directory", exc_info=True)


# ----- accounts -----

def user_store() -> UserStore:
    return UserStore(PROJECT_ROOT / "config" / "users.db")


def signed_in_user() -> dict | None:
    """The account in this session, if it still exists and its password has not been changed since sign-in."""
    if "user" not in session:
        return None
    user = user_store().get(session["user"])
    if user and secrets.compare_digest(session.get("user_key", ""), user["session_key"]):
        return user
    return None


def safe_next(target: str | None) -> str:
    """Return to the page that asked for sign-in, but never to another website."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target and target.isprintable():
        return target
    return url_for("dashboard")


@app.before_request
def require_sign_in():
    """Every page, file and API call needs a signed-in account; only the sign-in page and health check are open."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    g.user = signed_in_user()
    if g.user:
        return None
    if request.path.startswith(("/review/api/", "/assistant/")):
        return {"error": "Your sign-in has expired. Reload the page and sign in again."}, 401
    return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else None))


@app.route("/login", methods=["GET", "POST"])
def login():
    target = safe_next(request.values.get("next"))
    if signed_in_user():
        return redirect(target)
    store = user_store()
    username = request.form.get("username", "").strip()
    if request.method == "POST":
        check_csrf()
        user = store.authenticate(username, request.form.get("password", ""))
        if user:
            session.clear()
            session.permanent = True
            session["user"], session["user_key"] = user["username"], user["session_key"]
            return redirect(target)
        app.logger.warning("Failed sign-in for %r from %s", username, request.remote_addr)
        flash("That username and password do not match an account.", "error")
    return render_template("login.html", next=request.values.get("next", ""), username=username,
                           has_accounts=bool(store.list())), 401 if request.method == "POST" else 200


@app.post("/logout")
def logout():
    check_csrf()
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))


users_cli = AppGroup("users", help="Manage the accounts that can sign in to the dashboard.")
app.cli.add_command(users_cli)


@users_cli.command("add")
@click.argument("username")
@click.option("--name", help="Name shown on the dashboard and recorded in the review history. Defaults to the username.")
@click.password_option()
def add_user(username, name, password):
    """Create an account."""
    try:
        user_store().add(username, password, name)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Added {username}. They can now sign in.")


@users_cli.command("password")
@click.argument("username")
@click.password_option()
def reset_password(username, password):
    """Set a new password. The account is signed out everywhere."""
    try:
        user_store().set_password(username, password)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Changed the password for {username}.")


@users_cli.command("remove")
@click.argument("username")
def remove_user(username):
    """Delete an account. It is signed out at its next request."""
    if not user_store().remove(username):
        raise click.ClickException(f"There is no account named {username}.")
    click.echo(f"Removed {username}.")


@users_cli.command("list")
def list_users():
    """Show every account and when it last signed in."""
    users = user_store().list()
    if not users:
        click.echo("No accounts yet. Add one with: flask --app app users add <username>")
    for user in users:
        click.echo(f"{user['username']:<24} {user['name']:<30} last sign-in: {user['last_login_at'] or 'never'}")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}, 200


@app.get("/")
def dashboard():
    manager = file_manager()
    queued_files = [path.name for path in manager.list_input_files()]
    invoice_dir = manager.input_dir / "invoices"
    invoice_files = sorted(path.name for path in invoice_dir.iterdir() if path.is_file() and is_invoice(path.name)) if invoice_dir.exists() else []
    invoice_reports = sorted(manager.reports_dir.glob("telecom_invoices_*.xlsx"), reverse=True)
    master_path = PROJECT_ROOT / "Master.xlsx"
    history = run_history(PROJECT_ROOT)
    selected_run = request.args.get("run") or (history[0]["run_id"] if history else None)
    if selected_run and not RUN_PATTERN.fullmatch(selected_run):
        abort(404)
    errors = run_errors(PROJECT_ROOT, selected_run) if selected_run else {"rows": [], "count": 0, "available": False}
    return render_template(
        "index.html",
        master_exists=master_path.exists(),
        master_name=master_path.name,
        master_size=master_path.stat().st_size if master_path.exists() else 0,
        queued_files=queued_files,
        invoice_files=invoice_files,
        latest_invoice_report=invoice_reports[0].name if invoice_reports else None,
        latest_summary=history[0] if history else None,
        history=history,
        backups=list_backups(PROJECT_ROOT),
        selected_run=selected_run,
        errors=errors,
    )


@app.post("/upload-master")
@serialized_workbooks
def upload_master():
    upload = request.files.get("master_file")
    if not upload or not upload.filename:
        flash("Choose a master workbook first.", "error")
        return redirect(url_for("dashboard"))
    if Path(upload.filename).suffix.lower() != ".xlsx":
        flash("The master must be an .xlsx workbook. Macro-enabled .xlsm files are supported as input files only.", "error")
        return redirect(url_for("dashboard"))

    manager = file_manager()
    master_path = PROJECT_ROOT / "Master.xlsx"
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=PROJECT_ROOT, suffix=".xlsx", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        upload.save(temporary_path)
        workbook = load_workbook(temporary_path, read_only=True)
        workbook.close()
        if master_path.exists():
            manager.backup_master(master_path)
        os.replace(temporary_path, master_path)
        sync_master_to_data(master_path)
    except Exception:
        app.logger.exception("Unable to replace the master workbook")
        flash("The master could not be replaced. Choose a valid, readable .xlsx workbook. Your current master is unchanged.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    flash("Master workbook replaced successfully.", "success")
    return redirect(url_for("dashboard"))


@app.post("/upload-files")
@serialized_workbooks
def upload_files():
    uploads = request.files.getlist("data_files")
    manager = file_manager()
    saved_count = 0
    rejected = []
    for upload in uploads:
        safe_name = secure_filename(upload.filename or "")
        if not safe_name:
            continue
        if not is_allowed(safe_name):
            rejected.append(safe_name)
            continue
        destination = manager.input_dir / safe_name
        try:
            with destination.open("xb") as target:
                upload.save(target)
        except FileExistsError:
            flash(f"{safe_name} is already queued. The existing workbook was kept.", "error")
            continue
        saved_count += 1

    if saved_count:
        flash(f"{saved_count} workbook(s) added to the processing queue.", "success")
    if rejected:
        flash(f"Skipped unsupported file(s): {', '.join(rejected)}", "error")
    if not saved_count and not rejected and not any(upload.filename for upload in uploads):
        flash("Choose at least one workbook to upload.", "error")
    return redirect(url_for("dashboard"))


@app.post("/upload-invoices")
def upload_invoices():
    uploads = request.files.getlist("invoice_files")
    invoice_dir = file_manager().input_dir / "invoices"
    invoice_dir.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    rejected = []
    for upload in uploads:
        safe_name = secure_filename(upload.filename or "")
        if not safe_name:
            continue
        if not is_invoice(safe_name):
            rejected.append(safe_name)
            continue
        destination = invoice_dir / safe_name
        try:
            with destination.open("xb") as target:
                upload.save(target)
        except FileExistsError:
            flash(f"{safe_name} is already queued. The existing PDF was kept.", "error")
            continue
        saved_count += 1

    if saved_count:
        flash(f"{saved_count} invoice PDF(s) added for extraction.", "success")
    if rejected:
        flash(f"Skipped non-PDF file(s): {', '.join(rejected)}", "error")
    if not saved_count and not rejected and not any(upload.filename for upload in uploads):
        flash("Choose at least one invoice PDF.", "error")
    return redirect(url_for("dashboard"))


@app.post("/extract-invoices")
def extract_invoices():
    manager = file_manager()
    invoice_dir = manager.input_dir / "invoices"
    pdf_paths = sorted(path for path in invoice_dir.iterdir() if path.is_file() and is_invoice(path.name)) if invoice_dir.exists() else []
    if not pdf_paths:
        flash("Add at least one invoice PDF before extracting.", "error")
        return redirect(url_for("dashboard"))

    output_path = process_invoices(pdf_paths, manager.reports_dir)
    archive_dir = manager.processed_dir / "invoices"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for pdf_path in pdf_paths:
        destination = archive_dir / pdf_path.name
        if destination.exists():
            destination = archive_dir / f"{pdf_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.pdf"
        pdf_path.replace(destination)
    flash(f"Invoice extraction complete. Report created: {output_path.name}", "success")
    return redirect(url_for("dashboard"))


@app.post("/preview")
@serialized_workbooks
def preview_automation():
    check_csrf()
    file_manager()
    try:
        preview = preview_queue(PROJECT_ROOT, CONFIG_PATH)
    except (ValueError, OSError) as exc:
        flash(f"Preview could not be prepared: {exc}", "error")
        return redirect(url_for("dashboard"))
    token = confirmation_tokens("preview").dumps({"fingerprint": preview["fingerprint"]})
    return render_template("workbooks/preview.html", preview=preview, token=token)


@app.post("/run")
@serialized_workbooks
def run_automation():
    check_csrf()
    manager = file_manager()
    if not (PROJECT_ROOT / "Master.xlsx").exists():
        flash("Upload a master workbook before running automation.", "error")
        return redirect(url_for("dashboard"))
    if not manager.list_input_files():
        flash("Add at least one workbook to the processing queue first.", "error")
        return redirect(url_for("dashboard"))

    try:
        approved = confirmation_tokens("preview").loads(request.form.get("preview_token", ""), max_age=3600)
        if approved.get("fingerprint") != queue_fingerprint(PROJECT_ROOT, CONFIG_PATH):
            raise ValueError("The master, queue or configuration changed. Preview the changes again.")
    except (BadSignature, ValueError, OSError) as exc:
        flash(str(exc) if isinstance(exc, (ValueError, OSError)) else "Preview the changes again; the confirmation has expired or is invalid.", "error")
        return redirect(url_for("dashboard"))
    summary = AutomationRunner(PROJECT_ROOT, CONFIG_PATH).run()
    sync_master_to_data()
    flash(
        f"Run complete: {summary['processed_count']} processed, {summary['failed_count']} failed.",
        "success" if summary["failed_count"] == 0 else "error",
    )
    return redirect(url_for("dashboard"))


@app.route("/backups/<filename>/restore", methods=["GET", "POST"])
@serialized_workbooks
def restore_master(filename):
    try:
        selected = backup_path(PROJECT_ROOT, filename)
    except ValueError:
        abort(404)
    if request.method == "POST":
        check_csrf()
        try:
            approved = confirmation_tokens("restore").loads(request.form.get("restore_token", ""), max_age=3600)
            if approved.get("name") != filename:
                raise ValueError("The selected backup changed. Review the restore again.")
            safety_copy = restore_backup(PROJECT_ROOT, filename, approved.get("fingerprint", ""))
            sync_master_to_data()
        except (BadSignature, ValueError, OSError) as exc:
            flash(str(exc) if isinstance(exc, (ValueError, OSError)) else "Review the restore again; the confirmation has expired or is invalid.", "error")
            return redirect(url_for("dashboard", _anchor="backups"))
        except Exception:
            app.logger.exception("Backup restore failed")
            flash("This backup could not be restored. The current master is unchanged.", "error")
            return redirect(url_for("dashboard", _anchor="backups"))
        message = f"Restored {filename}."
        if safety_copy:
            message += f" Previous master saved as {safety_copy.name}."
        flash(message, "success")
        return redirect(url_for("dashboard", _anchor="backups"))
    try:
        fingerprint = restore_fingerprint(PROJECT_ROOT, selected)
        sheets = workbook_overview(selected)
    except Exception:
        flash("This backup is not a readable Excel workbook. Select another backup.", "error")
        return redirect(url_for("dashboard", _anchor="backups"))
    token = confirmation_tokens("restore").dumps({"name": filename, "fingerprint": fingerprint})
    return render_template("workbooks/restore.html", filename=filename, sheets=sheets, token=token,
                           master_exists=(PROJECT_ROOT / "Master.xlsx").exists())


@app.get("/backups/<filename>/download")
def download_backup(filename):
    try:
        selected = backup_path(PROJECT_ROOT, filename)
    except ValueError:
        abort(404)
    return send_file(selected, as_attachment=True, download_name=selected.name)


@app.get("/reports/<run_id>/<kind>")
def download_run_report(run_id, kind):
    suffix = {"errors": "_error_report.csv", "summary": "_summary.json"}.get(kind)
    if not RUN_PATTERN.fullmatch(run_id) or suffix is None:
        abort(404)
    path = PROJECT_ROOT / "reports" / f"{run_id}{suffix}"
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/download-master")
def download_master():
    master_path = PROJECT_ROOT / "Master.xlsx"
    if not master_path.exists():
        flash("There is no master workbook to download.", "error")
        return redirect(url_for("dashboard"))
    return send_file(master_path, as_attachment=True, download_name="Master.xlsx")


@app.get("/download-invoice-report/<filename>")
def download_invoice_report(filename: str):
    report_path = (file_manager().reports_dir / secure_filename(filename)).resolve()
    reports_dir = file_manager().reports_dir.resolve()
    if report_path.parent != reports_dir or report_path.suffix.lower() != ".xlsx" or not report_path.exists():
        flash("Invoice report was not found.", "error")
        return redirect(url_for("dashboard"))
    return send_file(report_path, as_attachment=True, download_name=report_path.name)


if __name__ == "__main__":
    # Port 5000 is taken by the macOS AirPlay Receiver, which answers with a blank page.
    app.run(debug=True, port=5050)
