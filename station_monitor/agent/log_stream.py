import asyncio
import glob
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable


LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b", re.IGNORECASE)


class LogStreamer:
    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self.emit = emit
        self.sequence = 0

    async def follow_journal(self, unit: str, source: str) -> None:
        while True:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-fu", unit, "-n", "200", "-o", "cat",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout
            try:
                while line := await proc.stdout.readline():
                    await self._line(source, line.decode(errors="replace").rstrip())
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    await proc.wait()
            await asyncio.sleep(2)

    async def follow_files(self, patterns: list[str]) -> None:
        positions: dict[Path, int] = {}
        while True:
            for pattern in patterns:
                for filename in glob.glob(pattern):
                    path = Path(filename)
                    try:
                        size = path.stat().st_size
                        position = positions.setdefault(path, max(0, size - 256 * 1024))
                        if size < position:
                            position = 0
                        if size > position:
                            with path.open("r", encoding="utf-8", errors="replace") as handle:
                                handle.seek(position)
                                for line in handle:
                                    await self._line(path.name, line.rstrip())
                                positions[path] = size
                    except (OSError, UnicodeError):
                        continue
            await asyncio.sleep(1)

    async def _line(self, source: str, message: str) -> None:
        if not message:
            return
        self.sequence += 1
        match = LEVEL_RE.search(message)
        level = (match.group(1).upper() if match else "INFO").replace("WARN", "WARNING").replace("CRITICAL", "FATAL")
        if (
            source == "task-actions.log"
            and level == "ERROR"
            and "CallCartesianPlan failed" in message
            and "violates limits" in message
        ):
            level = "WARNING"
            message = f"[可恢复：程序将自动回退到 PTP] {message}"
        await self.emit(
            {
                "type": "log",
                "payload": {
                    "timestamp": datetime.now(timezone.utc).isoformat(), "source": source,
                    "level": level, "sequence": self.sequence, "message": message,
                },
            }
        )
