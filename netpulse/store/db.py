"""SQLite feature store: schema, migrations, retention and roll-up.

One database file holds everything the agent knows: samples, scores,
incidents, user labels, the probe ledger and drift events. Keeping it in
SQLite rather than an embedded TSDB is a deliberate trade from PRD 6.1: it is
auditable with any sqlite3 binary, it exports trivially, and a user who wants
to verify the privacy claim can read the whole file themselves.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..features.schema import FEATURE_NAMES, MODELLED_FEATURES, sql_column_definitions
from ..logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1
ROLLUP_BUCKET_S = 300.0


def _rollup_column_definitions() -> str:
    parts: list[str] = []
    for name in FEATURE_NAMES:
        parts.append(f'"{name}_avg" REAL')
        if name in MODELLED_FEATURES:
            parts.append(f'"{name}_max" REAL')
    return ",\n    ".join(parts)


MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"""
        CREATE TABLE IF NOT EXISTS samples (
            ts REAL PRIMARY KEY,
            {sql_column_definitions()}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS samples_rollup (
            bucket_ts REAL PRIMARY KEY,
            n INTEGER NOT NULL,
            {_rollup_column_definitions()}
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scores (
            ts REAL PRIMARY KEY,
            health REAL NOT NULL,
            risk_5m REAL NOT NULL,
            risk_15m REAL NOT NULL,
            severity TEXT NOT NULL,
            l1_anomaly REAL NOT NULL DEFAULT 0,
            l2_anomaly REAL NOT NULL DEFAULT 0,
            primary_layer TEXT,
            secondary_layer TEXT,
            layer_scores TEXT,
            warming_up INTEGER NOT NULL DEFAULT 0,
            coverage REAL NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            ended_at REAL,
            primary_layer TEXT NOT NULL,
            secondary_layer TEXT,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            evidence TEXT,
            remediation TEXT,
            template_id TEXT,
            user_label TEXT,
            notified INTEGER NOT NULL DEFAULT 0,
            peak_risk REAL NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            verdict TEXT NOT NULL,
            window_start REAL NOT NULL,
            window_end REAL NOT NULL,
            note TEXT,
            incident_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS probe_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            collector TEXT NOT NULL,
            target TEXT NOT NULL,
            ok INTEGER NOT NULL,
            duration_ms REAL,
            detail TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS drift_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            feature TEXT NOT NULL,
            psi REAL NOT NULL,
            action TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scores_ts ON scores(ts)",
        "CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at)",
        "CREATE INDEX IF NOT EXISTS idx_incidents_active ON incidents(ended_at)",
        "CREATE INDEX IF NOT EXISTS idx_labels_window ON labels(window_start, window_end)",
        "CREATE INDEX IF NOT EXISTS idx_probe_log_ts ON probe_log(ts)",
    )
}


class Database:
    """Thread-safe wrapper around a single SQLite connection.

    The agent writes from collector threads and reads from the API thread. A
    single connection behind a re-entrant lock is simpler to reason about than
    a pool, and SQLite serialises writers regardless.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if self.path.name != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=15.0
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        if not read_only:
            self.migrate()

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=15000")

    # ------------------------------------------------------------------ core

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, [tuple(row) for row in rows])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ migrations

    def user_version(self) -> int:
        row = self.query_one("PRAGMA user_version")
        return int(row[0]) if row else 0

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        current = self.user_version()
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database at {self.path} was written by a newer NetPulse "
                f"(schema {current} > {SCHEMA_VERSION}); upgrade the agent or move the file"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(version, ())
            log.info("applying database migration %d (%d statements)", version, len(statements))
            with self.transaction() as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={version}")
        return self.user_version()

    # -------------------------------------------------------------- key store

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, time.time()),
        )

    # ------------------------------------------------------------- maintenance

    def rollup_and_prune(
        self,
        *,
        downsample_after_hours: int,
        raw_days: int,
        rollup_days: int,
        now: float | None = None,
    ) -> dict[str, int]:
        """Downsample old raw samples, then enforce the retention windows.

        Raw rows older than ``downsample_after_hours`` are aggregated into
        five-minute buckets and deleted. The bucket table is kept for
        ``rollup_days``. ``raw_days`` is the hard ceiling on raw rows for
        installs that turn downsampling far out, and it is applied second so a
        short raw window always wins.
        """
        now = time.time() if now is None else now
        raw_cutoff = now - downsample_after_hours * 3600.0
        hard_cutoff = now - raw_days * 86400.0
        rollup_cutoff = now - rollup_days * 86400.0
        stats = {"rolled": 0, "raw_deleted": 0, "rollup_deleted": 0}

        select_parts = [
            f"ROUND(ts / {ROLLUP_BUCKET_S}) * {ROLLUP_BUCKET_S} AS bucket_ts",
            "COUNT(*) AS n",
        ]
        insert_columns = ["bucket_ts", "n"]
        for name in FEATURE_NAMES:
            select_parts.append(f'AVG("{name}")')
            insert_columns.append(f'"{name}_avg"')
            if name in MODELLED_FEATURES:
                select_parts.append(f'MAX("{name}")')
                insert_columns.append(f'"{name}_max"')

        placeholders = ", ".join(select_parts)
        columns = ", ".join(insert_columns)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT OR REPLACE INTO samples_rollup ({columns}) "
                f"SELECT {placeholders} FROM samples WHERE ts < ? GROUP BY bucket_ts",
                (raw_cutoff,),
            )
            stats["rolled"] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            cursor = conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,))
            stats["raw_deleted"] = max(cursor.rowcount, 0)
            cursor = conn.execute("DELETE FROM samples WHERE ts < ?", (hard_cutoff,))
            stats["raw_deleted"] += max(cursor.rowcount, 0)
            cursor = conn.execute(
                "DELETE FROM samples_rollup WHERE bucket_ts < ?", (rollup_cutoff,)
            )
            stats["rollup_deleted"] = max(cursor.rowcount, 0)
            conn.execute("DELETE FROM scores WHERE ts < ?", (rollup_cutoff,))
            conn.execute("DELETE FROM probe_log WHERE ts < ?", (raw_cutoff,))
            conn.execute(
                "DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at < ?",
                (rollup_cutoff,),
            )
        return stats

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def wipe(self) -> None:
        """Delete every stored observation (PRD 12.1 one-click wipe)."""
        with self.transaction() as conn:
            for table in (
                "samples",
                "samples_rollup",
                "scores",
                "incidents",
                "labels",
                "probe_log",
                "drift_events",
            ):
                conn.execute(f"DELETE FROM {table}")
        self.vacuum()

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": str(self.path), "schema_version": self.user_version()}
        for table in (
            "samples",
            "samples_rollup",
            "scores",
            "incidents",
            "labels",
            "probe_log",
            "drift_events",
        ):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        try:
            out["size_bytes"] = self.path.stat().st_size
        except OSError:
            out["size_bytes"] = 0
        row = self.query_one("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM samples")
        out["oldest_sample"] = row["lo"] if row else None
        out["newest_sample"] = row["hi"] if row else None
        return out
