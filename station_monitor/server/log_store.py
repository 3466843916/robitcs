import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from .database import Database
from .models import LogPayload


SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class LogStore:
    def __init__(self, root: Path, db: Database):
        self.root = root
        self.db = db
        self._lock = asyncio.Lock()

    def path_for(self, station_id: str, source: str, log_date: date) -> Path:
        safe_source = SAFE_NAME.sub("_", source)
        return self.root / station_id / safe_source / f"{log_date.isoformat()}.ndjson"

    async def append(self, station_id: str, item: LogPayload) -> Path:
        path = self.path_for(station_id, item.source, item.timestamp.date())
        record = item.model_dump(mode="json") | {"station_id": station_id}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._append_sync, path, line)
            await self.db.execute(
                """INSERT INTO log_files(station_id,source,log_date,path,bytes) VALUES(?,?,?,?,?)
                ON CONFLICT(station_id,source,log_date) DO UPDATE SET bytes=excluded.bytes""",
                (station_id, item.source, item.timestamp.date().isoformat(), str(path), path.stat().st_size),
            )
        await self.db.execute(
            "INSERT INTO log_entries(station_id,source,level,timestamp,sequence,message) VALUES(?,?,?,?,?,?)",
            (station_id, item.source, item.level, item.timestamp.isoformat(), item.sequence, item.message),
        )
        if item.level in {"ERROR", "FATAL"}:
            await self.db.execute(
                "INSERT INTO log_errors(station_id,source,level,timestamp,sequence,message) VALUES(?,?,?,?,?,?)",
                (station_id, item.source, item.level, item.timestamp.isoformat(), item.sequence, item.message),
            )
        return path

    @staticmethod
    def _append_sync(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    async def query_errors(
        self, station_id: str | None, limit: int = 500
    ) -> list[dict]:
        if station_id:
            return await self.db.fetch_all(
                "SELECT * FROM log_errors WHERE station_id=? ORDER BY timestamp DESC, sequence DESC LIMIT ?",
                (station_id, limit),
            )
        return await self.db.fetch_all(
            "SELECT * FROM log_errors ORDER BY timestamp DESC, station_id, sequence DESC LIMIT ?",
            (limit,),
        )

    async def query_page(
        self, station_id: str | None, level: str | None, page: int, page_size: int,
        log_date: date | None = None, source_group: str | None = None,
    ) -> dict:
        filters: list[str] = ["substr(timestamp,1,10)=?"]
        params: list[object] = [(log_date or date.today()).isoformat()]
        if station_id:
            filters.append("station_id=?")
            params.append(station_id)
        if level:
            filters.append("level IN (\"ERROR\",\"FATAL\")" if level == "ERROR" else "level=?")
            if level != "ERROR":
                params.append(level)
        if source_group == "robot":
            filters.append("source=?")
            params.append("arm_app.log")
        elif source_group == "collection":
            filters.append("source IN (\"collection.log\",\"task-actions.log\")")
        where = " WHERE " + " AND ".join(filters) if filters else ""
        total_row = await self.db.fetch_one("SELECT count(*) value FROM log_entries" + where, tuple(params))
        offset = (page - 1) * page_size
        items = await self.db.fetch_all(
            "SELECT station_id,source,level,timestamp,sequence,message FROM log_entries" + where + " ORDER BY timestamp DESC,station_id,sequence DESC LIMIT ? OFFSET ?",
            tuple(params + [page_size, offset]),
        )
        total = int(total_row["value"] if total_row else 0)
        return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size)}

    async def clear_day(
        self, log_date: date, station_id: str | None = None, source_group: str | None = None,
    ) -> dict[str, int]:
        file_filters = ["log_date=?"]
        entry_filters = ["substr(timestamp,1,10)=?"]
        params: list[object] = [log_date.isoformat()]
        if station_id:
            file_filters.append("station_id=?")
            entry_filters.append("station_id=?")
            params.append(station_id)
        if source_group == "robot":
            file_filters.append("source=?")
            entry_filters.append("source=?")
            params.append("arm_app.log")
        elif source_group == "collection":
            condition = "source IN (\"collection.log\",\"task-actions.log\")"
            file_filters.append(condition)
            entry_filters.append(condition)
        file_where = " AND ".join(file_filters)
        entry_where = " AND ".join(entry_filters)
        root = self.root.resolve()
        async with self._lock:
            async with self.db.connect() as conn:
                cursor = await conn.execute(f"SELECT path FROM log_files WHERE {file_where}", tuple(params))
                rows = [dict(row) for row in await cursor.fetchall()]
                cursor = await conn.execute(f"DELETE FROM log_entries WHERE {entry_where}", tuple(params))
                deleted_entries = cursor.rowcount
                await conn.execute(f"DELETE FROM log_errors WHERE {entry_where}", tuple(params))
                await conn.execute(f"DELETE FROM log_files WHERE {file_where}", tuple(params))
                await conn.commit()
            deleted_files = 0
            for row in rows:
                candidate = Path(row["path"]).resolve()
                if root in candidate.parents:
                    await asyncio.to_thread(candidate.unlink, missing_ok=True)
                    deleted_files += 1
        return {"deleted": max(0, deleted_entries), "files_deleted": deleted_files}

    async def list_files(self, station_id: str | None = None) -> list[dict]:
        if station_id:
            return await self.db.fetch_all(
                "SELECT * FROM log_files WHERE station_id=? ORDER BY log_date DESC, source", (station_id,)
            )
        return await self.db.fetch_all("SELECT * FROM log_files ORDER BY log_date DESC, station_id, source")

    async def delete_files(self, paths: list[str]) -> int:
        unique_paths = list(dict.fromkeys(paths))
        placeholders = ",".join("?" for _ in unique_paths)
        rows = await self.db.fetch_all(
            f"SELECT station_id,source,log_date,path FROM log_files WHERE path IN ({placeholders})",
            tuple(unique_paths),
        )
        root = self.root.resolve()
        deleted = 0
        async with self._lock:
            async with self.db.connect() as conn:
                for row in rows:
                    candidate = Path(row["path"]).resolve()
                    if root not in candidate.parents:
                        continue
                    await asyncio.to_thread(candidate.unlink, missing_ok=True)
                    await conn.execute("DELETE FROM log_files WHERE path=?", (row["path"],))
                    params = (row["station_id"], row["source"], row["log_date"])
                    await conn.execute(
                        "DELETE FROM log_entries WHERE station_id=? AND source=? AND substr(timestamp,1,10)=?", params
                    )
                    await conn.execute(
                        "DELETE FROM log_errors WHERE station_id=? AND source=? AND substr(timestamp,1,10)=?", params
                    )
                    deleted += 1
                await conn.commit()
        return deleted

    async def stream_file(self, path: Path) -> AsyncIterator[bytes]:
        with path.open("rb") as handle:
            while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                yield chunk

    async def cleanup(self, retention_days: int) -> None:
        cutoff = date.today() - timedelta(days=retention_days)
        for path in self.root.glob("*/*/*.ndjson"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                path.unlink(missing_ok=True)
                await self.db.execute("DELETE FROM log_files WHERE path=?", (str(path),))
