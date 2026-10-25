"""SQLite account store with encrypted passwords and a run ledger.

Design rules:
- passwords are encrypted at rest with Fernet; the key lives outside the DB;
- logs and CLI output never include a decrypted password unless the operator
  explicitly asks for an export with secrets;
- one connection per thread.  The runner creates a Store per worker.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .mail import MailSession, MailSessionError
from .util import ConfigError, append_jsonl, now_iso, write_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    email TEXT NOT NULL,
    password_enc BLOB,
    status TEXT NOT NULL DEFAULT 'created',
    profile_id TEXT,
    proxy TEXT,
    proxy_enc BLOB,
    engine TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    verified_at TEXT,
    meta TEXT,
    artifact_dir TEXT,
    mail_session_enc BLOB,
    UNIQUE(service, email)
);
CREATE INDEX IF NOT EXISTS idx_accounts_service ON accounts(service);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    service TEXT,
    email TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    artifact_dir TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_run ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_service ON runs(service);
"""


@dataclass
class Account:
    id: int
    service: str
    email: str
    status: str
    profile_id: str | None
    proxy: str | None
    engine: str | None
    created_at: str
    updated_at: str
    verified_at: str | None
    meta: dict[str, Any]
    artifact_dir: str | None
    password_enc: bytes | None
    proxy_enc: bytes | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "email": self.email,
            "status": self.status,
            "profile_id": self.profile_id,
            "proxy": self.proxy,
            "engine": self.engine,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "verified_at": self.verified_at,
            "meta": self.meta,
            "artifact_dir": self.artifact_dir,
            "has_password": bool(self.password_enc),
            "has_proxy_credentials": bool(self.proxy_enc),
        }


def _load_key(settings: Settings) -> bytes:
    env_key = os.getenv("AUTOREG_FERNET_KEY")
    if env_key:
        try:
            Fernet(env_key.encode())
        except Exception as exc:
            raise ConfigError("AUTOREG_FERNET_KEY is not a valid Fernet key") from exc
        return env_key.encode()

    path = settings.paths.key
    if path.is_file():
        key = path.read_bytes().strip()
        try:
            Fernet(key)
        except Exception as exc:
            raise ConfigError(f"Invalid Fernet key in {path}: {exc}") from exc
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key + b"\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


class Store:
    def __init__(self, settings: Settings):
        settings.paths.ensure()
        self.settings = settings
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(settings.paths.db), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(accounts)")}
        if "proxy_enc" not in columns:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN proxy_enc BLOB")
        if "mail_session_enc" not in columns:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN mail_session_enc BLOB")
        self.conn.commit()
        self.fernet = Fernet(_load_key(settings))

    # -- crypto ----------------------------------------------------------
    def encrypt(self, password: str | None) -> bytes | None:
        if password is None:
            return None
        return self.fernet.encrypt(password.encode("utf-8"))

    def decrypt(self, token: bytes | None) -> str | None:
        if not token:
            return None
        try:
            return self.fernet.decrypt(bytes(token)).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigError("Cannot decrypt password: wrong key for this database") from exc

    # -- accounts --------------------------------------------------------
    def upsert_account(self, *, service: str, email: str, password: str | None,
                       status: str = "created", profile_id: str | None = None,
                       proxy: str | None = None, engine: str | None = None,
                       meta: dict[str, Any] | None = None,
                       artifact_dir: str | None = None,
                       proxy_secret: str | None = None,
                       mail_session: MailSession | None = None) -> int:
        now = now_iso()
        enc = self.encrypt(password)
        proxy_enc = self.encrypt(proxy_secret) if proxy_secret else None
        session_enc = self._encrypt_session(mail_session)
        payload = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM accounts WHERE service=? AND email=?", (service, email)
            ).fetchone()
            if row:
                self.conn.execute(
                    """UPDATE accounts SET password_enc=COALESCE(?, password_enc),
                       status=?, profile_id=COALESCE(?, profile_id),
                       proxy=COALESCE(?, proxy), proxy_enc=COALESCE(?, proxy_enc),
                       engine=COALESCE(?, engine),
                       updated_at=?, meta=?, artifact_dir=COALESCE(?, artifact_dir),
                       mail_session_enc=COALESCE(?, mail_session_enc)
                       WHERE id=?""",
                    (enc, status, profile_id, proxy, proxy_enc, engine, now, payload,
                     artifact_dir, session_enc, row["id"]),
                )
                return int(row["id"])
            cur = self.conn.execute(
                """INSERT INTO accounts
                   (service,email,password_enc,status,profile_id,proxy,proxy_enc,
                    engine,created_at,updated_at,meta,artifact_dir,mail_session_enc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (service, email, enc, status, profile_id, proxy, proxy_enc,
                 engine, now, now, payload, artifact_dir, session_enc),
            )
            return int(cur.lastrowid)

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return Account(
            id=row["id"], service=row["service"], email=row["email"],
            status=row["status"], profile_id=row["profile_id"],
            proxy=row["proxy"], engine=row["engine"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            verified_at=row["verified_at"], meta=meta,
            artifact_dir=row["artifact_dir"], password_enc=row["password_enc"],
            proxy_enc=row["proxy_enc"] if "proxy_enc" in row.keys() else None,
        )

    def get_account(self, account_id: int) -> Account | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def find_account(self, service: str, email: str) -> Account | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE service=? AND email=?", (service, email)
        ).fetchone()
        return self._row_to_account(row) if row else None

    def list_accounts(self, service: str | None = None,
                      status: str | None = None) -> list[Account]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        args: list[Any] = []
        if service:
            sql += " AND service=?"
            args.append(service)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC"
        return [self._row_to_account(r) for r in self.conn.execute(sql, args).fetchall()]

    def get_password(self, account_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT password_enc FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        return self.decrypt(row["password_enc"]) if row else None

    def get_proxy(self, account_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT proxy_enc FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        return self.decrypt(row["proxy_enc"]) if row else None

    # -- mail session ----------------------------------------------------
    def _encrypt_session(self, session: MailSession | None) -> bytes | None:
        if session is None:
            return None
        payload = json.dumps(session.to_dict(), ensure_ascii=False)
        return self.fernet.encrypt(payload.encode("utf-8"))

    def get_mail_session(self, account_id: int) -> MailSession | None:
        row = self.conn.execute(
            "SELECT mail_session_enc FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row or row["mail_session_enc"] is None:
            return None
        try:
            payload = self.fernet.decrypt(bytes(row["mail_session_enc"]))
            return MailSession.from_dict(json.loads(payload.decode("utf-8")))
        except (InvalidToken, ValueError, TypeError, UnicodeDecodeError,
                MailSessionError) as exc:
            raise MailSessionError("Cannot restore stored mail session") from exc

    def set_mail_session(self, account_id: int, session: MailSession) -> None:
        encrypted = self._encrypt_session(session)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE accounts SET mail_session_enc=?, updated_at=? WHERE id=?",
                (encrypted, now_iso(), account_id),
            )
            if cur.rowcount == 0:
                raise ConfigError(f"Account {account_id} not found")

    def mark_status(self, account_id: int, status: str, *,
                    verified: bool = False, error: str | None = None) -> None:
        now = now_iso()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE accounts SET status=?, updated_at=?,
                   verified_at=CASE WHEN ? THEN ? ELSE verified_at END,
                   meta=json_set(COALESCE(meta,'{}'), '$.last_error', ?)
                   WHERE id=?""",
                (status, now, 1 if verified else 0, now, error, account_id),
            )

    def delete_account(self, account_id: int) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            return cur.rowcount > 0

    def count(self, service: str | None = None) -> int:
        if service:
            row = self.conn.execute("SELECT COUNT(*) c FROM accounts WHERE service=?", (service,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()
        return int(row["c"])

    # -- ledger ----------------------------------------------------------
    def record_run(self, *, run_id: str, service: str, email: str, status: str,
                   started_at: str, finished_at: str | None = None,
                   error: str | None = None, artifact_dir: str | None = None,
                   meta: dict[str, Any] | None = None) -> None:
        event = {
            "run_id": run_id, "service": service, "email": email,
            "status": status, "started_at": started_at,
            "finished_at": finished_at, "error": error,
            "artifact_dir": artifact_dir, "meta": meta or {},
        }
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO runs (run_id,service,email,status,started_at,
                   finished_at,error,artifact_dir,meta) VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, service, email, status, started_at, finished_at,
                 error, artifact_dir, json.dumps(meta or {}, ensure_ascii=False)),
            )
        append_jsonl(self.settings.paths.runs / "ledger.jsonl", event)
        # A bounded human-readable log too; JSONL is the source of truth.
        append_jsonl(self.settings.paths.runs / f"{run_id}.jsonl", event)

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- export ----------------------------------------------------------
    def export(self, path: str | Path, *, include_secrets: bool = False,
               service: str | None = None) -> Path:
        p = Path(path)
        accounts = self.list_accounts(service=service)
        rows: list[dict[str, Any]] = []
        for acc in accounts:
            row = acc.public()
            if include_secrets:
                row["password"] = self.get_password(acc.id)
            rows.append(row)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix.lower() == ".jsonl":
            with open(p, "w", encoding="utf-8", newline="") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        elif p.suffix.lower() == ".csv":
            fields = ["id", "service", "email", "status", "profile_id", "proxy",
                      "engine", "created_at", "updated_at", "verified_at"]
            if include_secrets:
                fields.append("password")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        else:
            write_json(p, rows)
        return p

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
