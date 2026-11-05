"""Review workspace: check extracted invoice data against the PDF, then confirm it and export it to Excel.

The routes are grouped into pages (document list and review screen), the JSON API used by the review screen,
form posts from the document list, and files (page images, original PDFs, Excel exports). Business logic lives in
src/review_workspace.py.
"""
import math
import re
import secrets
import threading
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

from src.review_export import EXPORT_NAME_PATTERN
from src.review_schema import parse_number
from src.review_workspace import (
    ACTION_TITLES,
    BULK_ACTIONS,
    TAB_STATUSES,
    DocumentNotFound,
    ReadOnlyDocument,
    ReviewWorkspace,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "review_config.json"
TABS = [
    ("to_review", "To review"),
    ("postponed", "Postponed"),
    ("confirmed", "Confirmed"),
    ("exported", "Exported"),
    ("rejected", "Rejected"),
    ("deleted", "Deleted"),
    ("all", "All"),
]
TAB_STAMPS = {
    "confirmed": ("confirmed_at", "confirmed_by", "Confirmed"),
    "exported": ("exported_at", "exported_by", "Exported"),
    "rejected": ("rejected_at", "rejected_by", "Rejected"),
    "deleted": ("deleted_at", "deleted_by", "Deleted"),
}
UPLOADED_STAMP = ("created_at", "created_by", "Uploaded")
LIST_DEFAULTS = {"queue": "all", "tab": "to_review", "q": "", "label": "", "sort": "created_at", "dir": "desc"}

review_bp = Blueprint("review", __name__, url_prefix="/review")
_workspace_lock = threading.Lock()


def workspace() -> ReviewWorkspace:
    with _workspace_lock:
        if "review_workspace" not in current_app.extensions:
            current_app.extensions["review_workspace"] = ReviewWorkspace(
                PROJECT_ROOT,
                Path(current_app.config.get("REVIEW_CONFIG_PATH", CONFIG_PATH)),
                background=not current_app.config.get("REVIEW_SYNC_IMPORTS", False),
            )
        return current_app.extensions["review_workspace"]


def reviewer() -> str:
    """The signed-in account's name, recorded in the activity log and on confirmations."""
    user = g.get("user")
    return user["name"] if user else workspace().config.get("default_reviewer", "Reviewer")


def csrf_token() -> str:
    if "review_csrf" not in session:
        session["review_csrf"] = secrets.token_urlsafe(16)
    return session["review_csrf"]


def check_csrf() -> None:
    expected = session.get("review_csrf")
    if not expected or not secrets.compare_digest(request.form.get("csrf", ""), expected):
        abort(400, "The form expired. Reload the page and try again.")


def list_args(source) -> dict:
    """The document list's filters and sort order, taken from a query string or form and checked."""
    ws = workspace()
    args = {key: (source.get(key) or default).strip() for key, default in LIST_DEFAULTS.items()}
    if args["queue"] != "all" and args["queue"] not in ws.queues:
        args["queue"] = "all"
    if args["tab"] not in TAB_STATUSES:
        args["tab"] = "to_review"
    if args["sort"] not in ws.sort_keys:
        args["sort"] = "created_at"
    if args["dir"] not in ("asc", "desc"):
        args["dir"] = "desc"
    return args


def list_query(args: dict, **overrides) -> dict:
    """List arguments that differ from the defaults, for short URLs."""
    merged = {**args, **overrides}
    return {key: value for key, value in merged.items() if value != LIST_DEFAULTS.get(key)}


def indian_amount(value) -> str:
    number = parse_number(str(value)) if value not in (None, "") else None
    if number is None:
        return "" if value in (None, "") else str(value)
    whole, _, fraction = f"{abs(number):.2f}".partition(".")
    head, groups = whole[:-3], [whole[-3:]]
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return f"{'-' if number < 0 else ''}{','.join(groups)}.{fraction}"


@review_bp.app_template_filter("review_value")
def review_value(value, definition: dict | None = None) -> str:
    if value is None or value == "":
        return ""
    kind = (definition or {}).get("type")
    if kind == "number":
        return indian_amount(value)
    if kind == "date":
        try:
            return date.fromisoformat(value).strftime("%d %b %Y")
        except ValueError:
            return value
    return value


@review_bp.app_template_filter("review_stamp")
def review_stamp(value) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return value


@review_bp.app_context_processor
def review_template_helpers():
    return {"review_csrf_token": csrf_token}


# ----- pages -----

@review_bp.get("/")
def index():
    return redirect(url_for("review.documents"))


@review_bp.get("/documents")
def documents():
    ws = workspace()
    args = list_args(request.args)
    everything = ws.store.list_documents()
    matching = ws.list_documents(**args, documents=everything)
    page_size = int(ws.config.get("page_size", 50))
    page_count = max(1, math.ceil(len(matching) / page_size))
    page = min(max(request.args.get("page", 1, type=int), 1), page_count)

    review_args = {**args, "tab": "to_review", "q": "", "label": ""}
    waiting = [document for document in ws.list_documents(**review_args, documents=everything) if document["status"] == "to_review"]
    start_url = url_for("review.document", document_id=waiting[0]["id"], **list_query(review_args)) if waiting else None

    queue_workspace = next((space["name"] for space in ws.workspaces if any(queue["id"] == args["queue"] for queue in space["queues"])), None)
    rows = [ws.list_row(document) for document in matching[(page - 1) * page_size: page * page_size]]
    return render_template(
        "review/dashboard.html",
        ws=ws,
        args=args,
        rows=rows,
        show_labels=any(row["labels"] for row in rows),
        total=len(matching),
        page=page,
        page_count=page_count,
        page_size=page_size,
        counts=ws.counts(everything),
        labels=ws.labels_in_use(everything),
        tabs=TABS,
        tab_label=dict(TABS)[args["tab"]],
        stamp=TAB_STAMPS.get(args["tab"], UPLOADED_STAMP),
        bulk_actions=[(action, ACTION_TITLES[action]) for action in BULK_ACTIONS[args["tab"]]],
        queue_title=ws.queues.get(args["queue"], "All documents"),
        queue_workspace=queue_workspace,
        start_url=start_url,
        list_query=list_query,
        reviewer=reviewer(),
    )


@review_bp.get("/documents/<int:document_id>")
def document(document_id: int):
    ws = workspace()
    try:
        stored = ws.document(document_id)
    except DocumentNotFound:
        abort(404)
    args = list_args(request.args)
    ids = [item["id"] for item in ws.list_documents(**args)]
    position = ids.index(document_id) if document_id in ids else None

    def neighbour(offset: int) -> str | None:
        if position is None or not 0 <= position + offset < len(ids):
            return None
        return url_for("review.document", document_id=ids[position + offset], **list_query(args))

    serialized = ws.serialize(stored)
    boot = {
        "document": serialized,
        "schema": ws.schema,
        "threshold": ws.threshold,
        "queues": [{"id": queue_id, "name": name} for queue_id, name in ws.queues.items()],
        "position": {"index": position + 1, "total": len(ids)} if position is not None else None,
        "focus": request.args.get("field"),
        "reviewer": reviewer(),
        "urls": {
            "base": url_for("review.document", document_id=document_id),
            "api": url_for("review.api_document", document_id=document_id),
            "original": url_for("review.original", document_id=document_id),
            "export": url_for("review.download_export", filename="FILE"),
            "list": url_for("review.documents", **list_query(args)),
            "prev": neighbour(-1),
            "next": neighbour(1),
        },
    }
    return render_template("review/document.html", boot=boot, document=serialized, reviewer=reviewer(), action_titles=ACTION_TITLES)


# ----- JSON API used by the review screen -----

def api_error(error: str, status: int = 400, **extra):
    return jsonify(error=error, **extra), status


def api_route(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        # Requiring a JSON body means another website cannot trigger these with a plain form post.
        if request.method != "GET" and not request.is_json:
            return api_error("Send the request body as JSON.", 415)
        try:
            return view(*args, **kwargs)
        except DocumentNotFound as exc:
            return api_error(str(exc), 404)
        except ReadOnlyDocument as exc:
            return api_error(str(exc), 409)
        except (ValueError, IndexError, TypeError) as exc:
            return api_error(str(exc), 400)
    return wrapper


def json_body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object.")
    return payload


@review_bp.get("/api/documents/<int:document_id>")
@api_route
def api_document(document_id: int):
    ws = workspace()
    return jsonify(ws.serialize(ws.document(document_id)))


@review_bp.patch("/api/documents/<int:document_id>/fields")
@api_route
def api_update_field(document_id: int):
    payload = json_body()
    if not isinstance(payload.get("path"), str):
        raise ValueError("Say which field to update.")
    return jsonify(workspace().update_value(document_id, payload["path"], payload, reviewer()))


@review_bp.post("/api/documents/<int:document_id>/tables/<table_id>/rows")
@api_route
def api_add_row(document_id: int, table_id: str):
    return jsonify(workspace().add_row(document_id, table_id, reviewer()))


@review_bp.delete("/api/documents/<int:document_id>/tables/<table_id>/rows/<int:index>")
@api_route
def api_delete_row(document_id: int, table_id: str, index: int):
    return jsonify(workspace().delete_row(document_id, table_id, index, reviewer()))


@review_bp.post("/api/documents/<int:document_id>/capture")
@api_route
def api_capture(document_id: int):
    payload = json_body()
    bbox = payload.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("Send the box as [x0, top, x1, bottom].")
    return jsonify(workspace().capture(document_id, int(payload.get("page") or 0), bbox))


@review_bp.post("/api/documents/<int:document_id>/actions/<action>")
@api_route
def api_action(document_id: int, action: str):
    ws = workspace()
    payload = json_body()
    result = ws.perform(document_id, action, reviewer(), note=payload.get("note", ""), queue=payload.get("queue"), label=payload.get("label"))
    body = {"ok": result.ok, "message": result.message, "messages": result.messages}
    if result.export_file:
        body["export_url"] = url_for("review.download_export", filename=result.export_file)
    if not result.ok:
        return api_error(result.message, 400, **body)
    body["document"] = ws.serialize(ws.document(document_id))
    return jsonify(body)


@review_bp.get("/api/documents/<int:document_id>/activity")
@api_route
def api_activity(document_id: int):
    ws = workspace()
    ws.document(document_id)
    return jsonify(ws.store.activity(document_id))


@review_bp.post("/api/documents/<int:document_id>/time")
@api_route
def api_time(document_id: int):
    ws = workspace()
    ws.document(document_id)
    seconds = float(json_body().get("seconds") or 0)
    if 0 < seconds <= 3600:
        ws.store.add_time(document_id, seconds)
    return "", 204


# ----- forms posted from the document list -----

@review_bp.post("/upload")
def upload():
    check_csrf()
    ws = workspace()
    args = list_args(request.form)
    queue = request.form.get("queue")
    if queue not in ws.queues:
        queue = args["queue"] if args["queue"] in ws.queues else ws.default_queue

    saved, rejected = 0, []
    for upload in request.files.getlist("documents"):
        if not upload or not upload.filename:
            continue
        file_name = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if Path(file_name).suffix.lower() not in ws.accepted_extensions:
            rejected.append(file_name)
            continue
        ws.add_upload(upload, file_name, queue, reviewer())
        saved += 1

    if saved:
        flash(f"{saved} document(s) added to {ws.queues[queue]}. The data is being extracted.", "success")
    if rejected:
        flash(f"Skipped unsupported file(s): {', '.join(rejected)}. Upload PDF files.", "error")
    if not saved and not rejected:
        flash("Choose at least one PDF to upload.", "error")
    return redirect(url_for("review.documents", **list_query(args, tab="to_review")))


@review_bp.post("/bulk")
def bulk():
    check_csrf()
    ws = workspace()
    args = list_args(request.form)
    back = redirect(url_for("review.documents", **list_query(args)))
    action = request.form.get("action", "")
    ids = [int(value) for value in request.form.getlist("ids") if value.isdigit()]
    if not ids:
        flash("Select at least one document first.", "error")
        return back

    if action == "export":
        exported, path = ws.export(ids, reviewer())
        if path:
            flash(path.name, "export")
        if len(exported) < len(ids):
            flash(f"{len(ids) - len(exported)} document(s) skipped: only confirmed documents can be exported.", "error")
        return back
    if action not in ACTION_TITLES:
        flash("Choose an action.", "error")
        return back

    done, problems = 0, []
    for document_id in ids:
        try:
            result = ws.perform(
                document_id,
                action,
                reviewer(),
                note=request.form.get("note", ""),
                queue=request.form.get("target_queue"),
                label=request.form.get("label"),
            )
        except DocumentNotFound:
            continue
        if result.ok:
            done += 1
        else:
            problems.append(result.message)
    if done:
        flash(f"{ACTION_TITLES[action]}: {done} document(s) updated.", "success")
    if problems:
        flash(f"{len(problems)} document(s) skipped. {problems[0]}", "error")
    return back


# ----- files -----

@review_bp.get("/documents/<int:document_id>/pages/<int:number>.png")
def page_image(document_id: int, number: int):
    try:
        path = workspace().page_image(document_id, number, "thumb" if request.args.get("size") == "thumb" else "full")
    except (DocumentNotFound, IndexError):
        abort(404)
    return send_file(path, mimetype="image/png", max_age=7 * 24 * 3600)


@review_bp.get("/documents/<int:document_id>/original")
def original(document_id: int):
    ws = workspace()
    try:
        stored = ws.document(document_id)
    except DocumentNotFound:
        abort(404)
    return send_file(
        ws.document_path(stored),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=stored["file_name"],
    )


@review_bp.get("/exports/<filename>")
def download_export(filename: str):
    path = workspace().reports_dir / filename
    if not re.fullmatch(EXPORT_NAME_PATTERN, filename) or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)
