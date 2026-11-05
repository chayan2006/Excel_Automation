"""Business logic of the review workspace: imports, the document lifecycle, list queries and edits.

A document moves through these statuses:

    importing -> to_review -> confirmed -> exported
                     |  ^
                     v  |
                  postponed            (any of them) -> rejected / deleted

Only "to_review" and "postponed" documents can be edited. Confirmed, exported and rejected documents can be
returned to review, and deleted documents restored.
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .document_extractor import capture_text, extract_document, render_page
    from .review_export import export_documents
    from .review_schema import (
        blank_state,
        field_definition,
        iter_fields,
        load_schema,
        normalize_value,
        parse_number,
        review_queue,
        validate,
    )
    from .review_store import ReviewStore, now
except ImportError:  # pragma: no cover
    from document_extractor import capture_text, extract_document, render_page
    from review_export import export_documents
    from review_schema import (
        blank_state,
        field_definition,
        iter_fields,
        load_schema,
        normalize_value,
        parse_number,
        review_queue,
        validate,
    )
    from review_store import ReviewStore, now


STATUS_LABELS = {
    "importing": "Importing",
    "failed_import": "Import failed",
    "to_review": "To review",
    "postponed": "Postponed",
    "confirmed": "Confirmed",
    "exported": "Exported",
    "rejected": "Rejected",
    "deleted": "Deleted",
}
TAB_STATUSES = {
    "to_review": ("importing", "failed_import", "to_review"),
    "postponed": ("postponed",),
    "confirmed": ("confirmed",),
    "exported": ("exported",),
    "rejected": ("rejected",),
    "deleted": ("deleted",),
    "all": ("importing", "failed_import", "to_review", "postponed", "confirmed", "exported", "rejected"),
}
EDITABLE_STATUSES = {"to_review", "postponed"}
LABEL_STATUSES = {"failed_import", "to_review", "postponed", "confirmed", "exported", "rejected"}
ACTION_STATUSES = {
    "confirm": EDITABLE_STATUSES,
    "postpone": {"to_review"},
    "reject": {"failed_import", "to_review", "postponed"},
    "delete": {"failed_import", "to_review", "postponed", "confirmed", "exported", "rejected"},
    "restore": {"deleted"},
    "reopen": {"postponed", "confirmed", "exported", "rejected"},
    "export": {"confirmed", "exported"},
    "reextract": {"failed_import", "to_review", "postponed"},
    "move": LABEL_STATUSES,
    "add_label": LABEL_STATUSES,
    "remove_label": LABEL_STATUSES,
}
ACTION_TITLES = {
    "confirm": "Confirm",
    "postpone": "Postpone",
    "reject": "Reject",
    "delete": "Delete",
    "restore": "Restore",
    "reopen": "Return to review",
    "export": "Export to Excel",
    "reextract": "Re-extract data",
    "move": "Move to queue",
    "add_label": "Add label",
    "remove_label": "Remove label",
}
ACTION_LOG_NAMES = {
    "confirm": "confirmed",
    "postpone": "postponed",
    "reject": "rejected",
    "delete": "deleted",
    "restore": "restored",
    "reopen": "reopened",
    "reextract": "reextracted",
    "move": "moved",
    "add_label": "label_added",
    "remove_label": "label_removed",
}
BULK_ACTIONS = {
    "to_review": ["postpone", "reject", "move", "add_label", "remove_label", "delete"],
    "postponed": ["reopen", "reject", "move", "add_label", "remove_label", "delete"],
    "confirmed": ["export", "reopen", "move", "add_label", "remove_label", "delete"],
    "exported": ["export", "reopen", "add_label", "remove_label", "delete"],
    "rejected": ["reopen", "add_label", "remove_label", "delete"],
    "deleted": ["restore"],
    "all": ["export", "postpone", "reject", "move", "add_label", "remove_label", "delete"],
}
META_SORTS = {"created_at", "modified_at", "file_name", "status", "confirmed_at", "exported_at", "rejected_at", "deleted_at"}
SERIALIZED_KEYS = (
    "id", "queue", "file_name", "status", "vendor", "has_text", "page_count", "pages", "fields", "tables", "labels",
    "note", "automated", "time_spent", "export_file", "created_at", "created_by", "modified_at", "modified_by",
    "confirmed_at", "confirmed_by", "exported_at", "exported_by", "rejected_at", "rejected_by", "deleted_at", "deleted_by",
)
THUMBNAIL_RESOLUTION = 24


class DocumentNotFound(LookupError):
    pass


class ReadOnlyDocument(Exception):
    pass


@dataclass
class ActionResult:
    ok: bool
    message: str
    messages: list = field(default_factory=list)
    export_file: str | None = None


def all_validated(fields: dict, tables: dict) -> tuple[dict, dict]:
    fields = {key: {**state, "validated": True} for key, state in fields.items()}
    tables = {
        key: [{column: {**cell, "validated": True} for column, cell in row.items()} for row in rows]
        for key, rows in tables.items()
    }
    return fields, tables


class ReviewWorkspace:
    def __init__(self, project_root: Path, config_path: Path, background: bool = True):
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.schema = load_schema(config_path.parent / self.config.get("schema_file", "review_schema.json"))
        storage_dir = project_root / self.config.get("storage_dir", "output/review")
        self.files_dir = storage_dir / "files"
        self.pages_dir = storage_dir / "pages"
        self.reports_dir = project_root / "reports"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.store = ReviewStore(storage_dir / "review.db")
        self.store.fail_interrupted_imports()

        self.workspaces = self.config["workspaces"]
        self.queues = {queue["id"]: queue["name"] for space in self.workspaces for queue in space["queues"]}
        self.default_queue = self.config.get("default_queue") if self.config.get("default_queue") in self.queues else next(iter(self.queues))
        self.threshold = float(self.config.get("score_threshold", 0.9))
        self.accepted_extensions = {extension.lower() for extension in self.config.get("accepted_extensions", [".pdf"])}
        self.list_columns = [
            definition
            for definition in (field_definition(self.schema, field_id) for field_id in self.config.get("list_columns", []))
            if definition and definition.get("type") != "table"
        ]
        self.sort_keys = META_SORTS | {field["id"] for field in iter_fields(self.schema) if field.get("type") != "table"}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="review-import") if background else None

    # ----- documents and files -----

    def document(self, document_id: int) -> dict:
        document = self.store.get(document_id)
        if document is None:
            raise DocumentNotFound(f"Document {document_id} does not exist.")
        return document

    def document_path(self, document: dict) -> Path:
        return self.files_dir / document["stored_name"]

    def add_upload(self, upload, file_name: str, queue: str, actor: str) -> int:
        """Save an uploaded file (a werkzeug FileStorage) and start extracting its data."""
        stored_name = f"{uuid.uuid4().hex}{Path(file_name).suffix.lower()}"
        upload.save(self.files_dir / stored_name)
        document_id = self.store.create(queue, file_name, stored_name, actor)
        self.schedule_import(document_id)
        return document_id

    def schedule_import(self, document_id: int) -> None:
        if self._executor:
            self._executor.submit(self.import_document, document_id)
        else:
            self.import_document(document_id)

    def import_document(self, document_id: int) -> None:
        document = self.store.get(document_id)
        if document is None:
            return
        try:
            result = extract_document(self.document_path(document), self.schema, int(self.config.get("max_scan_pages", 60)))
        except Exception as exc:  # pragma: no cover - a broken file must not stop the import thread
            self.store.update(document_id, status="failed_import", note=f"The data could not be extracted: {exc}")
            self.store.log(document_id, "System", "import_failed", str(exc))
            return

        fields, tables = result["fields"], result["tables"]
        automated = self._automate(result)
        changes = {}
        if automated:
            fields, tables = all_validated(fields, tables)
            changes = {"confirmed_at": now(), "confirmed_by": "Automation"}
        self.store.update(
            document_id,
            status="confirmed" if automated else "to_review",
            vendor=result["vendor"],
            has_text=int(result["has_text"]),
            page_count=result["page_count"],
            pages=result["pages"],
            fields=fields,
            tables=tables,
            automated=int(automated),
            note=None,
            **changes,
        )
        to_check = review_queue(self.schema, result["fields"], result["tables"], self.threshold)
        self.store.log(document_id, "System", "imported", {"vendor": result["vendor"], "pages": result["page_count"], "to_check": len(to_check)})
        if automated:
            self.store.log(document_id, "Automation", "confirmed", "Every value passed the checks.")

    def _automate(self, result: dict) -> bool:
        """automation_level "never" sends everything to review; "confident" confirms documents where every value is
        above the score threshold and every check passes; "always" confirms everything without errors."""
        level = self.config.get("automation_level", "never")
        if level not in ("confident", "always") or not result["has_text"]:
            return False
        messages = validate(self.schema, result["fields"], result["tables"], result["has_text"])
        if any(message["level"] == "error" for message in messages):
            return False
        if level == "always":
            return True
        return not messages and not review_queue(self.schema, result["fields"], result["tables"], self.threshold)

    def page_image(self, document_id: int, number: int, size: str = "full") -> Path:
        document = self.document(document_id)
        resolution = THUMBNAIL_RESOLUTION if size == "thumb" else int(self.config.get("page_resolution", 150))
        target = self.pages_dir / str(document_id) / f"{number}-{size}.png"
        if not target.exists():
            render_page(self.document_path(document), number, target, resolution)
        return target

    def capture(self, document_id: int, page: int, bbox: list) -> dict:
        document = self.document(document_id)
        text, tight_bbox = capture_text(self.document_path(document), page, [float(value) for value in bbox])
        return {"text": text, "bbox": tight_bbox}

    # ----- lists -----

    def list_documents(self, queue="all", tab="to_review", q="", label="", sort="created_at", dir="desc", documents=None) -> list[dict]:
        statuses = TAB_STATUSES.get(tab, TAB_STATUSES["to_review"])
        needles = {q.lower(), q.lower().replace(",", "")} - {""}
        matching = []
        for document in self.store.list_documents() if documents is None else documents:
            if document["status"] not in statuses or (queue != "all" and document["queue"] != queue):
                continue
            if label and label not in document["labels"]:
                continue
            if needles and not self._matches(document, needles):
                continue
            matching.append(document)
        return self._sorted(matching, sort if sort in self.sort_keys else "created_at", dir == "desc")

    @staticmethod
    def _matches(document: dict, needles: set) -> bool:
        haystack = [document["file_name"], document.get("vendor") or "", str(document["id"]), *document["labels"]]
        haystack += [(state or {}).get("value") or "" for state in document["fields"].values()]
        return any(needle in text.lower() for text in haystack for needle in needles)

    def _sorted(self, documents: list[dict], sort: str, descending: bool) -> list[dict]:
        definition = None if sort in META_SORTS else field_definition(self.schema, sort)

        def key(document):
            if definition is None:
                value = document.get(sort) or ""
                return value.lower() if sort == "file_name" else value
            value = (document["fields"].get(sort) or {}).get("value", "")
            if definition.get("type") == "number":
                return parse_number(value) if value else None
            return value.lower() or None

        present = [document for document in documents if key(document) not in (None, "")]
        missing = [document for document in documents if key(document) in (None, "")]
        present.sort(key=lambda document: (key(document), document["id"]), reverse=descending)
        missing.sort(key=lambda document: document["id"], reverse=descending)
        return present + missing

    def counts(self, documents: list[dict]) -> dict:
        counts = {queue: dict.fromkeys(TAB_STATUSES, 0) for queue in [*self.queues, "all"]}
        for document in documents:
            for tab, statuses in TAB_STATUSES.items():
                if document["status"] in statuses:
                    counts["all"][tab] += 1
                    if document["queue"] in counts:
                        counts[document["queue"]][tab] += 1
        return counts

    @staticmethod
    def labels_in_use(documents: list[dict]) -> list[str]:
        return sorted({label for document in documents for label in document["labels"]}, key=str.lower)

    def list_row(self, document: dict) -> dict:
        importing = document["status"] == "importing"
        messages = [] if importing else validate(self.schema, document["fields"], document["tables"], document["has_text"])
        editable = document["status"] in EDITABLE_STATUSES
        return {
            **{key: document.get(key) for key in SERIALIZED_KEYS if key not in ("pages", "fields", "tables")},
            "status_label": STATUS_LABELS.get(document["status"], document["status"]),
            "queue_name": self.queues.get(document["queue"], document["queue"]),
            "cells": {column["id"]: (document["fields"].get(column["id"]) or {}).get("value", "") for column in self.list_columns},
            "to_check": len(review_queue(self.schema, document["fields"], document["tables"], self.threshold)) if editable else 0,
            "errors": sum(message["level"] == "error" for message in messages),
            "warnings": sum(message["level"] == "warning" for message in messages),
        }

    # ----- one document -----

    def serialize(self, document: dict) -> dict:
        editable = document["status"] in EDITABLE_STATUSES
        importing = document["status"] == "importing"
        return {
            **{key: document.get(key) for key in SERIALIZED_KEYS},
            **self._review_state(document, editable, importing),
            "queue_name": self.queues.get(document["queue"], document["queue"]),
            "status_label": STATUS_LABELS.get(document["status"], document["status"]),
            "read_only": not editable,
            "actions": [action for action, statuses in ACTION_STATUSES.items() if document["status"] in statuses],
        }

    def _review_state(self, document: dict, editable: bool = True, importing: bool = False) -> dict:
        return {
            "fields": document["fields"],
            "tables": document["tables"],
            "messages": [] if importing else validate(self.schema, document["fields"], document["tables"], document["has_text"]),
            "needs_review": review_queue(self.schema, document["fields"], document["tables"], self.threshold) if editable else [],
            "modified_at": document.get("modified_at"),
        }

    def _editable(self, document_id: int) -> dict:
        document = self.document(document_id)
        if document["status"] not in EDITABLE_STATUSES:
            status = STATUS_LABELS.get(document["status"], document["status"]).lower()
            raise ReadOnlyDocument(f"This document is {status}, so it cannot be edited. Return it to review first.")
        return document

    def label_for(self, path: str) -> str:
        parts = path.split(".")
        table = field_definition(self.schema, parts[0])
        if len(parts) == 1:
            return table["label"]
        column = field_definition(self.schema, path)
        return f"{table['label']} row {int(parts[1]) + 1}, {column['label']}"

    def update_value(self, document_id: int, path: str, payload: dict, actor: str) -> dict:
        """Save a reviewer's change to one field or table cell: a typed value, a box captured on the page, or just
        a confirmation that the extracted value is right. Any of them marks the value as validated."""
        definition = field_definition(self.schema, path)
        if definition is None or definition.get("type") == "table":
            raise ValueError(f"Unknown field: {path}")
        with self.store.lock:
            document = self._editable(document_id)
            parts = path.split(".")
            if len(parts) == 1:
                container, key = document["fields"], path
            else:
                rows = document["tables"].setdefault(parts[0], [])
                index = int(parts[1])
                if not 0 <= index < len(rows):
                    raise ValueError(f"Row {index + 1} does not exist.")
                container, key = rows[index], parts[2]

            state = {**blank_state(), **(container.get(key) or {})}
            previous = state["value"]
            if "value" in payload:
                state["value"], _ = normalize_value(definition, payload["value"])
                if state["value"] != previous:
                    state["source"] = "human"
            if payload.get("bbox"):
                page = int(payload.get("page") or 0)
                if not 1 <= page <= document["page_count"]:
                    raise ValueError(f"Page {page} does not exist.")
                state.update(page=page, bbox=[round(float(value), 2) for value in payload["bbox"][:4]], source="capture")
            state["validated"] = True
            container[key] = state

            self.store.update(document_id, actor, fields=document["fields"], tables=document["tables"])
            if state["value"] != previous:
                self.store.log(document_id, actor, "field_updated", {"path": path, "label": self.label_for(path), "from": previous, "to": state["value"]})
            return self._review_state(self.document(document_id))

    def _table(self, table_id: str) -> dict:
        table = field_definition(self.schema, table_id)
        if table is None or table.get("type") != "table":
            raise ValueError(f"Unknown table: {table_id}")
        return table

    def add_row(self, document_id: int, table_id: str, actor: str) -> dict:
        table = self._table(table_id)
        with self.store.lock:
            document = self._editable(document_id)
            rows = document["tables"].setdefault(table_id, [])
            rows.append({column["id"]: blank_state() for column in table["columns"]})
            self.store.update(document_id, actor, tables=document["tables"])
            self.store.log(document_id, actor, "row_added", {"table": table["label"], "row": len(rows)})
            return self._review_state(self.document(document_id))

    def delete_row(self, document_id: int, table_id: str, index: int, actor: str) -> dict:
        table = self._table(table_id)
        with self.store.lock:
            document = self._editable(document_id)
            rows = document["tables"].setdefault(table_id, [])
            if not 0 <= index < len(rows):
                raise ValueError(f"Row {index + 1} does not exist.")
            rows.pop(index)
            self.store.update(document_id, actor, tables=document["tables"])
            self.store.log(document_id, actor, "row_deleted", {"table": table["label"], "row": index + 1})
            return self._review_state(self.document(document_id))

    # ----- actions -----

    def perform(self, document_id: int, action: str, actor: str, note: str = "", queue: str | None = None, label: str | None = None) -> ActionResult:
        if action == "export":
            exported, path = self.export([document_id], actor)
            if not exported:
                return ActionResult(False, "Only confirmed documents can be exported.")
            return ActionResult(True, f"Exported to {path.name}.", export_file=path.name)
        if action not in ACTION_STATUSES:
            return ActionResult(False, f"Unknown action: {action}")

        note = " ".join((note or "").split()) or None
        with self.store.lock:
            document = self.document(document_id)
            status = document["status"]
            if status not in ACTION_STATUSES[action]:
                return ActionResult(False, f"{ACTION_TITLES[action]} is not available for {STATUS_LABELS.get(status, status).lower()} documents.")

            stamp = now()
            detail = note
            if action == "confirm":
                errors = [message for message in validate(self.schema, document["fields"], document["tables"], document["has_text"]) if message["level"] == "error"]
                if errors:
                    return ActionResult(False, "Fix the highlighted errors before confirming.", errors)
                fields, tables = all_validated(document["fields"], document["tables"])
                self.store.update(document_id, actor, status="confirmed", confirmed_at=stamp, confirmed_by=actor, fields=fields, tables=tables, note=None)
            elif action == "postpone":
                self.store.update(document_id, actor, status="postponed", note=note)
            elif action == "reject":
                self.store.update(document_id, actor, status="rejected", rejected_at=stamp, rejected_by=actor, note=note)
            elif action == "delete":
                self.store.update(document_id, actor, status="deleted", previous_status=status, deleted_at=stamp, deleted_by=actor)
            elif action == "restore":
                self.store.update(document_id, actor, status=document["previous_status"] or "to_review", previous_status=None, deleted_at=None, deleted_by=None)
            elif action == "reopen":
                self.store.update(document_id, actor, status="to_review", note=None)
            elif action == "reextract":
                self.store.update(document_id, actor, status="importing", note=None, automated=0)
            elif action == "move":
                if queue not in self.queues:
                    return ActionResult(False, "Choose a queue to move the document to.")
                if queue == document["queue"]:
                    return ActionResult(True, f"The document is already in {self.queues[queue]}.")
                self.store.update(document_id, actor, queue=queue)
                detail = {"from": self.queues.get(document["queue"], document["queue"]), "to": self.queues[queue]}
            else:
                label = " ".join((label or "").split())[:40]
                if not label:
                    return ActionResult(False, "Enter a label.")
                labels = set(document["labels"])
                if action == "add_label":
                    labels.add(label)
                else:
                    labels.discard(label)
                self.store.update(document_id, actor, labels=sorted(labels, key=str.lower))
                detail = label
            self.store.log(document_id, actor, ACTION_LOG_NAMES[action], detail)

        if action == "reextract":
            self.schedule_import(document_id)
        return ActionResult(True, f"{ACTION_TITLES[action]}: done.")

    def export(self, document_ids: list[int], actor: str) -> tuple[list[int], Path | None]:
        with self.store.lock:
            documents = [self.store.get(document_id) for document_id in document_ids]
            documents = [document for document in documents if document and document["status"] in ACTION_STATUSES["export"]]
            if not documents:
                return [], None
            path = export_documents(documents, self.schema, self.queues, self.reports_dir)
            stamp = now()
            for document in documents:
                self.store.update(document["id"], actor, status="exported", exported_at=stamp, exported_by=actor, export_file=path.name)
                self.store.log(document["id"], actor, "exported", path.name)
        return [document["id"] for document in documents], path
