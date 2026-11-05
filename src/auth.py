"""Accounts that can sign in to the web dashboard, stored in SQLite with salted password hashes.

Accounts are created by an administrator with `flask --app app users add`; there is no public sign-up.
"""
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


MIN_PASSWORD_LENGTH = 8
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# Checked when the username is unknown, so a wrong username takes as long to reject as a wrong password.
_UNUSED_HASH = generate_password_hash(secrets.token_urlsafe(16))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    session_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")


class UserStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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

    def get(self, username: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT username, name, created_at, last_login_at FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def add(self, username: str, password: str, name: str | None = None) -> dict:
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("Use 1-64 letters, digits, dots, dashes, underscores or @ for the username.")
        check_password(password)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users (username, name, password_hash, session_key, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, " ".join((name or username).split())[:60], generate_password_hash(password), secrets.token_urlsafe(16), now()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"An account named {username} already exists.") from None
        return self.get(username)

    def set_password(self, username: str, password: str) -> None:
        """Change the password and sign the account out everywhere it is signed in."""
        check_password(password)
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE users SET password_hash = ?, session_key = ? WHERE username = ?",
                (generate_password_hash(password), secrets.token_urlsafe(16), username),
            ).rowcount
        if not changed:
            raise ValueError(f"There is no account named {username}.")

    def remove(self, username: str) -> bool:
        with self._connect() as connection:
            return connection.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.get(username.strip())
        if not check_password_hash(user["password_hash"] if user else _UNUSED_HASH, password) or not user:
            return None
        with self._connect() as connection:
            connection.execute("UPDATE users SET last_login_at = ? WHERE username = ?", (now(), user["username"]))
        return user
