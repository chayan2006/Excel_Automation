"""SQLite storage for documents in the review workspace."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


JSON_COLUMNS = {"pages": list, "fields": dict, "tables": dict, "labels": list}
COLUMNS = {
    "queue", "file_name", "stored_name", "status", "previous_status", "vendor", "has_text", "page_count",
    "pages", "fields", "tables", "labels", "note", "automated", "time_spent", "export_file",
    "modified_at", "modified_by", "confirmed_at", "confirmed_by", "exported_at", "exported_by",
    "rejected_at", "rejected_by", "deleted_at", "deleted_by",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_status TEXT,
    vendor TEXT,
    has_text INTEGER NOT NULL DEFAULT 1,
    page_count INTEGER NOT NULL DEFAULT 0,
    pages TEXT NOT NULL DEFAULT '[]',
    fields TEXT NOT NULL DEFAULT '{}',
    tables TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    automated INTEGER NOT NULL DEFAULT 0,
    time_spent REAL NOT NULL DEFAULT 0,
    export_file TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    modified_at TEXT,
    modified_by TEXT,
    confirmed_at TEXT,
    confirmed_by TEXT,
    exported_at TEXT,
    exported_by TEXT,
    rejected_at TEXT,
    rejected_by TEXT,
    deleted_at TEXT,
    deleted_by TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS activity_document ON activity(document_id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ReviewStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        # Held around every read-modify-write of a document so concurrent saves never overwrite each other.
        self.lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA_SQL)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        document = dict(row)
        for column, empty in JSON_COLUMNS.items():
            if column in document:
                document[column] = json.loads(document[column]) if document[column] else empty()
        document["automated"] = bool(document.get("automated"))
        document["has_text"] = bool(document.get("has_text", 1))
        return document

    def create(self, queue: str, file_name: str, stored_name: str, actor: str) -> int:
        stamp = now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO documents (queue, file_name, stored_name, status, created_at, created_by, modified_at, modified_by)"
                " VALUES (?, ?, ?, 'importing', ?, ?, ?, ?)",
                (queue, file_name, stored_name, stamp, actor, stamp, actor),
            )
            document_id = cursor.lastrowid
        self.log(document_id, actor, "uploaded", file_name)
        return document_id

    def get(self, document_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return self._decode(row) if row else None

    def list_documents(self) -> list[dict]:
        """Every document without its page sizes, which can be long and are only needed on the review screen."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, queue, file_name, stored_name, status, previous_status, vendor, has_text, page_count,"
                " fields, tables, labels, note, automated, time_spent, export_file, created_at, created_by,"
                " modified_at, modified_by, confirmed_at, confirmed_by, exported_at, exported_by,"
                " rejected_at, rejected_by, deleted_at, deleted_by FROM documents"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, document_id: int, actor: str | None = None, **changes) -> None:
        unknown = set(changes) - COLUMNS
        if unknown:
            raise ValueError(f"Unknown document columns: {', '.join(sorted(unknown))}")
        if actor is not None:
            changes.setdefault("modified_by", actor)
        changes.setdefault("modified_at", now())
        values = [json.dumps(value) if column in JSON_COLUMNS else value for column, value in changes.items()]
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._connect() as connection:
            connection.execute(f"UPDATE documents SET {assignments} WHERE id = ?", (*values, document_id))

    def add_time(self, document_id: int, seconds: float) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE documents SET time_spent = time_spent + ? WHERE id = ?", (seconds, document_id))

    def log(self, document_id: int, actor: str | None, action: str, detail=None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO activity (document_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                (document_id, now(), actor, action, None if detail is None else json.dumps(detail)),
            )

    def activity(self, document_id: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT at, actor, action, detail FROM activity WHERE document_id = ? ORDER BY id DESC", (document_id,)
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else None} for row in rows]

    def fail_interrupted_imports(self) -> int:
        """Imports run in background threads; if the app stopped mid-import, let the reviewer re-run it."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE documents SET status = 'failed_import',"
                " note = 'The import was interrupted when the app stopped. Use Re-extract data to run it again.'"
                " WHERE status = 'importing'"
            )
            return cursor.rowcount
