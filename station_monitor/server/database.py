import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS stations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, ip TEXT NOT NULL UNIQUE,
  ssh_username TEXT NOT NULL DEFAULT "root", ssh_port INTEGER NOT NULL DEFAULT 22,
  ssh_authenticated INTEGER NOT NULL DEFAULT 0, ssh_reachable INTEGER NOT NULL DEFAULT 0,
  last_ssh_check TEXT,
  ros_domain_id INTEGER NOT NULL DEFAULT 0, joint_topic TEXT NOT NULL DEFAULT "/joint_states",
  notes TEXT NOT NULL DEFAULT "", deployment_message TEXT,
  acquisition_project_id INTEGER,
  deployment_status TEXT NOT NULL DEFAULT 'registered', certificate_fingerprint TEXT,
  temperature_topics TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_latest (
  station_id TEXT PRIMARY KEY REFERENCES stations(id) ON DELETE CASCADE,
  received_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarms (
  id TEXT PRIMARY KEY, station_id TEXT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
  alarm_key TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', first_at TEXT NOT NULL, last_at TEXT NOT NULL,
  acknowledged_at TEXT, recovered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarms_status ON alarms(status, last_at DESC);
CREATE TABLE IF NOT EXISTS command_jobs (
  id TEXT PRIMARY KEY, station_id TEXT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
  target TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, finished_at TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS log_files (
  station_id TEXT NOT NULL, source TEXT NOT NULL, log_date TEXT NOT NULL,
  path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(station_id, source, log_date)
);
CREATE TABLE IF NOT EXISTS log_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL,
  source TEXT NOT NULL, level TEXT NOT NULL, timestamp TEXT NOT NULL,
  sequence INTEGER NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_errors_time ON log_errors(timestamp DESC);
CREATE TABLE IF NOT EXISTS log_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL,
  source TEXT NOT NULL, level TEXT NOT NULL, timestamp TEXT NOT NULL,
  sequence INTEGER NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_entries_page ON log_entries(timestamp DESC, station_id, sequence DESC);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.executescript(SCHEMA)
            migrations = [
                "ALTER TABLE stations ADD COLUMN ros_domain_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE stations ADD COLUMN joint_topic TEXT NOT NULL DEFAULT \"/joint_states\"",
                "ALTER TABLE stations ADD COLUMN notes TEXT NOT NULL DEFAULT \"\"",
                "ALTER TABLE stations ADD COLUMN deployment_message TEXT",
                "ALTER TABLE stations ADD COLUMN ssh_username TEXT NOT NULL DEFAULT \"root\"",
                "ALTER TABLE stations ADD COLUMN ssh_port INTEGER NOT NULL DEFAULT 22",
                "ALTER TABLE stations ADD COLUMN ssh_authenticated INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE stations ADD COLUMN ssh_reachable INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE stations ADD COLUMN last_ssh_check TEXT",
                "ALTER TABLE stations ADD COLUMN acquisition_project_id INTEGER",
            ]
            for statement in migrations:
                try:
                    await conn.execute(statement)
                except aiosqlite.OperationalError:
                    pass
            await conn.execute(
                "UPDATE stations SET ssh_authenticated=1 WHERE deployment_status IN (\"installed\", \"connected\", \"ssh_connected\")"
            )
            await conn.execute("DELETE FROM alarms WHERE station_id NOT IN (SELECT id FROM stations)")
            await conn.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.connect() as conn:
            cursor = await conn.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        async with self.connect() as conn:
            await conn.execute(query, params)
            await conn.commit()

    async def audit(self, action: str, target: str | None, detail: dict[str, Any]) -> None:
        await self.execute(
            "INSERT INTO audit_log(timestamp,action,target,detail) VALUES(?,?,?,?)",
            (datetime.now(UTC).isoformat(), action, target, json.dumps(detail, ensure_ascii=False)),
        )

    async def cleanup(self, retention_days: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        async with self.connect() as conn:
            await conn.execute("DELETE FROM log_errors WHERE timestamp < ?", (cutoff,))
            await conn.execute("DELETE FROM log_entries WHERE timestamp < ?", (cutoff,))
            await conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
            await conn.execute("DELETE FROM alarms WHERE last_at < ? AND status='recovered'", (cutoff,))
            await conn.commit()
