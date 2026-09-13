"""db.py — database abstraction layer for λ-xTB job storage.

Currently implemented: SQLiteDatabase.
To swap to PostgreSQL: implement PostgreSQLDatabase with the same interface
and point DATABASE_URL at a postgresql:// connection string.

DATABASE_URL formats:
    sqlite:///data/lambda.db          relative path (default)
    sqlite:////absolute/path/db       absolute path
    postgresql://user:pass@host/db    future
"""

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import IntEnum


# ── status enum ───────────────────────────────────────────────────────────────

class JobStatus(IntEnum):
    ERROR      = -1  # terminal failure (not a workflow stage — negative by design)
    PENDING    =  0  # queued, not yet started
    PROCESSING =  1  # xTB is actively running
    DONE       =  2  # result ready, not yet viewed
    SEEN       =  3  # result viewed at least once


# ── abstract interface ────────────────────────────────────────────────────────

class Database(ABC):
    """Abstract database interface. All methods are synchronous."""

    @abstractmethod
    def init_db(self) -> None:
        """Create tables and indexes if they don't exist."""

    @abstractmethod
    def find_all_by_canonical(self, smiles_canonical: str) -> list[dict]:
        """Return all DONE or SEEN job rows for this canonical SMILES, newest first."""

    @abstractmethod
    def store_job(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        results: dict,
        xyz_neutral: str,
        xyz_cation: str,
        xyz_anion: str,
        email: str | None = None,
    ) -> None:
        """Persist a completed job (status=DONE)."""

    @abstractmethod
    def store_error(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        error_message: str,
        email: str | None = None,
    ) -> None:
        """Persist a failed job (status=ERROR)."""

    @abstractmethod
    def get_job(self, job_uuid: str) -> dict | None:
        """Return a job row by UUID, or None if not found."""

    @abstractmethod
    def mark_seen(self, job_uuid: str) -> None:
        """Transition a DONE job to SEEN (idempotent, ignores other statuses)."""

    @abstractmethod
    def create_pending_job(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        email: str | None = None,
    ) -> None:
        """Insert a new job row with status=PENDING."""

    @abstractmethod
    def mark_processing(self, job_uuid: str) -> None:
        """Transition PENDING -> PROCESSING (idempotent, no-op otherwise)."""

    @abstractmethod
    def update_result(
        self,
        job_uuid: str,
        results: dict,
        xyz_neutral: str,
        xyz_cation: str,
        xyz_anion: str,
    ) -> None:
        """Transition PENDING/PROCESSING -> DONE, storing results + geometries."""

    @abstractmethod
    def update_error(self, job_uuid: str, error_message: str) -> None:
        """Transition PENDING/PROCESSING -> ERROR, storing the error message."""

    @abstractmethod
    def get_status(self, job_uuid: str) -> dict | None:
        """Return {status, created_at} for a job, or None if not found."""


# ── SQL statements ────────────────────────────────────────────────────────────

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    uuid             TEXT    PRIMARY KEY,
    smiles_input     TEXT    NOT NULL,
    smiles_canonical TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    status           INTEGER NOT NULL DEFAULT 0,
    lambda_plus_eV   REAL,
    lambda_minus_eV  REAL,
    partial_json     TEXT,
    xyz_neutral      TEXT,
    xyz_cation       TEXT,
    xyz_anion        TEXT,
    error_message    TEXT,
    email            TEXT
)
"""

_DDL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_canonical ON jobs (smiles_canonical)
"""


# ── SQLite implementation ─────────────────────────────────────────────────────

class SQLiteDatabase(Database):
    """SQLite-backed implementation of the Database interface.

    A new connection is opened and closed for each operation — safe for
    single-process multi-threaded Flask (one connection per request thread).

    To migrate to PostgreSQL later:
      - Replace sqlite3.connect() with psycopg2.connect() in _connect()
      - Change PLACEHOLDER from '?' to '%s'
      - Adjust _DDL_TABLE types if needed (TEXT→VARCHAR, REAL→FLOAT8)
    """

    PLACEHOLDER = "?"   # swap to '%s' for psycopg2

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _q(self, n: int = 1) -> str:
        """Return n comma-separated placeholders, e.g. '?,?,?'."""
        return ",".join([self.PLACEHOLDER] * n)

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(_DDL_TABLE)
            # Migration: replace unique index with non-unique (allows multiple
            # runs per molecule for reproducibility statistics)
            conn.execute("DROP INDEX IF EXISTS idx_canonical")
            conn.execute(_DDL_INDEX)
            conn.commit()
        finally:
            conn.close()

    def find_all_by_canonical(self, smiles_canonical: str) -> list[dict]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE smiles_canonical = {self.PLACEHOLDER}"
                f" AND status IN ({self._q(2)})"
                f" ORDER BY created_at DESC",
                (smiles_canonical, int(JobStatus.DONE), int(JobStatus.SEEN)),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def store_job(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        results: dict,
        xyz_neutral: str,
        xyz_cation: str,
        xyz_anion: str,
        email: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO jobs
                    (uuid, smiles_input, smiles_canonical, created_at, status,
                     lambda_plus_eV, lambda_minus_eV,
                     partial_json, xyz_neutral, xyz_cation, xyz_anion, email)
                    VALUES ({self._q(12)})""",
                (
                    job_uuid, smiles_input, smiles_canonical, now, int(JobStatus.DONE),
                    results["lambda_plus_eV"], results["lambda_minus_eV"],
                    json.dumps(results["partial"]),
                    xyz_neutral, xyz_cation, xyz_anion,
                    email,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def store_error(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        error_message: str,
        email: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO jobs
                    (uuid, smiles_input, smiles_canonical, created_at, status,
                     error_message, email)
                    VALUES ({self._q(7)})""",
                (
                    job_uuid, smiles_input, smiles_canonical, now, int(JobStatus.ERROR),
                    error_message, email,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_job(self, job_uuid: str) -> dict | None:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM jobs WHERE uuid = {self.PLACEHOLDER}",
                (job_uuid,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def mark_seen(self, job_uuid: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE jobs SET status = {self.PLACEHOLDER}"
                f" WHERE uuid = {self.PLACEHOLDER} AND status = {self.PLACEHOLDER}",
                (int(JobStatus.SEEN), job_uuid, int(JobStatus.DONE)),
            )
            conn.commit()
        finally:
            conn.close()

    def create_pending_job(
        self,
        job_uuid: str,
        smiles_input: str,
        smiles_canonical: str,
        email: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO jobs
                    (uuid, smiles_input, smiles_canonical, created_at, status, email)
                    VALUES ({self._q(6)})""",
                (job_uuid, smiles_input, smiles_canonical, now, int(JobStatus.PENDING), email),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_processing(self, job_uuid: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE jobs SET status = {self.PLACEHOLDER}"
                f" WHERE uuid = {self.PLACEHOLDER} AND status = {self.PLACEHOLDER}",
                (int(JobStatus.PROCESSING), job_uuid, int(JobStatus.PENDING)),
            )
            conn.commit()
        finally:
            conn.close()

    def update_result(
        self,
        job_uuid: str,
        results: dict,
        xyz_neutral: str,
        xyz_cation: str,
        xyz_anion: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"""UPDATE jobs SET
                    status = {self.PLACEHOLDER},
                    lambda_plus_eV = {self.PLACEHOLDER},
                    lambda_minus_eV = {self.PLACEHOLDER},
                    partial_json = {self.PLACEHOLDER},
                    xyz_neutral = {self.PLACEHOLDER},
                    xyz_cation = {self.PLACEHOLDER},
                    xyz_anion = {self.PLACEHOLDER}
                    WHERE uuid = {self.PLACEHOLDER}
                    AND status IN ({self._q(2)})""",
                (
                    int(JobStatus.DONE),
                    results["lambda_plus_eV"], results["lambda_minus_eV"],
                    json.dumps(results["partial"]),
                    xyz_neutral, xyz_cation, xyz_anion,
                    job_uuid,
                    int(JobStatus.PENDING), int(JobStatus.PROCESSING),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def update_error(self, job_uuid: str, error_message: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"""UPDATE jobs SET status = {self.PLACEHOLDER}, error_message = {self.PLACEHOLDER}
                    WHERE uuid = {self.PLACEHOLDER}
                    AND status IN ({self._q(2)})""",
                (
                    int(JobStatus.ERROR), error_message,
                    job_uuid,
                    int(JobStatus.PENDING), int(JobStatus.PROCESSING),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_status(self, job_uuid: str) -> dict | None:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT status, created_at FROM jobs WHERE uuid = {self.PLACEHOLDER}",
                (job_uuid,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None


# ── factory ───────────────────────────────────────────────────────────────────

def get_db() -> Database:
    """Return a Database instance configured from the DATABASE_URL env var.

    Defaults to SQLite at data/lambda.db (relative to working directory).

    Examples:
        DATABASE_URL=sqlite:///data/lambda.db       (default)
        DATABASE_URL=sqlite:////tmp/lambda.db        (absolute path)
    """
    url = os.environ.get("DATABASE_URL", "sqlite:///data/lambda.db")
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        return SQLiteDatabase(path)
    raise ValueError(
        f"Unsupported DATABASE_URL scheme: {url!r}\n"
        "Supported: sqlite:///path/to/db"
    )
