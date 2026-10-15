import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def uid() -> str:
    return uuid4().hex


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${hashed}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, salt, expected = encoded.split("$")
        if method != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as db:
            db.executescript(files("circlepath").joinpath("schema.sql").read_text())
        path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def user(self, username: str) -> dict | None:
        user = self.one("SELECT * FROM users WHERE username=?", (username,))
        if user:
            user["trained_roles"] = json.loads(user["trained_roles"])
        return user

    def authenticate(self, username: str, password: str) -> bool:
        row = self.user(username)
        # Run a hash even for an unknown username to reduce timing differences.
        encoded = row["password_hash"] if row else hash_password("unknown-account-placeholder")
        return bool(verify_password(password, encoded) and row)

    @staticmethod
    def audit(db, case_id: str | None, actor: str, action: str, subject_id: str = ""):
        db.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?)",
            (uid(), case_id, actor, action, subject_id, stamp()),
        )

    @staticmethod
    def notify(db, case_id: str, recipient: str, kind: str, message: str, dedupe: str) -> bool:
        result = db.execute(
            "INSERT OR IGNORE INTO notifications "
            "(id,case_id,recipient,kind,message,dedupe,created_at) VALUES (?,?,?,?,?,?,?)",
            (uid(), case_id, recipient, kind, message, dedupe, stamp()),
        )
        return result.rowcount == 1
