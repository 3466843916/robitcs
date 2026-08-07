import ast
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable


LOGGER = logging.getLogger("station-monitor-agent.sdk")
TUPLE_PATTERN = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|:)??\s*(\([^\r\n]*\))\s*$")


class SdkCollector:
    def __init__(self, motor_command: str, joint_command: str, emit: Callable[[dict], Awaitable[None]], eef_motor_command: str = "arm-p7-sdk examples run airbot_example_get_eef_motor_states"):
        self.motor_command = motor_command
        self.joint_command = joint_command
        self.eef_motor_command = eef_motor_command
        self.emit = emit
        self.joints: dict[str, float] = {}
        self.temperatures: dict[str, float] = {}
        self.eef_temperatures: dict[str, float] = {}
        self.error_ids: tuple[int, ...] = ()
        self.healthy_sources: set[str] = set()
        self.last_data_at: dict[str, float] = {}
        self.started_at = time.monotonic()
        self.sequence = 0

    def snapshot(self) -> tuple[dict[str, float], dict[str, float]]:
        return dict(self.joints), {**self.temperatures, **self.eef_temperatures}

    def arm_data_fresh(self, max_age: float = 5.0, startup_grace: float = 10.0) -> bool:
        now = time.monotonic()
        if now - self.started_at < startup_grace:
            return True
        return all(now - self.last_data_at.get(source, 0) <= max_age for source in {"arm-sdk-motors", "arm-sdk-joints"})

    async def run(self) -> None:
        await asyncio.gather(
            self._supervise(self.motor_command, "arm-sdk-motors", self._parse_motor_line),
            self._supervise(self.joint_command, "arm-sdk-joints", self._parse_joint_line),
            self._supervise(self.eef_motor_command, "arm-sdk-eef-motors", self._parse_eef_line),
        )

    async def _supervise(self, command: str, source: str, parser: Callable[[str], Awaitable[None]]) -> None:
        while True:
            process = None
            try:
                self.healthy_sources.discard(source)
                self.last_data_at.pop(source, None)
                if source == "arm-sdk-motors": self.temperatures = {}
                elif source == "arm-sdk-joints": self.joints = {}
                elif source == "arm-sdk-eef-motors": self.eef_temperatures = {}
                process = await asyncio.create_subprocess_exec(
                    "/bin/bash", "-lc", command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdout
                assert process.stderr
                stderr_task = asyncio.create_task(self._forward_stderr(process.stderr, source))
                while line := await process.stdout.readline():
                    await parser(line.decode(errors="replace").strip())
                await process.wait()
                stderr_task.cancel()
                await self._log(source, "ERROR", f"SDK 采集进程退出，状态码 {process.returncode}，2 秒后重启")
            except asyncio.CancelledError:
                if process and process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
            except Exception as exc:
                await self._log(source, "ERROR", f"SDK 采集失败：{exc}，2 秒后重试")
            await asyncio.sleep(2)

    async def _forward_stderr(self, stream: asyncio.StreamReader, source: str) -> None:
        while line := await stream.readline():
            message = line.decode(errors="replace").strip()
            if message:
                await self._log(source, "ERROR", message)

    async def _parse_motor_line(self, line: str) -> None:
        parsed = self.parse_tuple_line(line)
        if not parsed:
            return
        name, values = parsed
        if name == "motor_temperatures":
            self.temperatures = {f"motor_{index + 1}": float(value) for index, value in enumerate(values)}
            await self._mark_healthy("arm-sdk-motors")
        elif name == "error_ids":
            current = tuple(int(value) for value in values)
            if current != self.error_ids:
                failed = {f"motor_{index + 1}": error for index, error in enumerate(current) if error}
                if failed:
                    await self._log("arm-sdk-motors", "ERROR", f"机械臂电机错误码：{failed}")
                elif self.error_ids and any(self.error_ids):
                    await self._log("arm-sdk-motors", "INFO", "机械臂电机错误码已恢复为 0")
                self.error_ids = current

    async def _parse_eef_line(self, line: str) -> None:
        parsed = self.parse_tuple_line(line)
        if not parsed or parsed[0] not in {"motor_temperatures", "eef_motor_temperatures", "eef_motor_temp", "temperatures", "temperature"}:
            return
        self.eef_temperatures = {f"eef_motor_{index + 1}": float(value) for index, value in enumerate(parsed[1])}
        if self.eef_temperatures:
            await self._mark_healthy("arm-sdk-eef-motors")

    async def _parse_joint_line(self, line: str) -> None:
        parsed = self.parse_tuple_line(line)
        if parsed and parsed[0] == "joint_pos":
            self.joints = {f"joint_{index + 1}": float(value) for index, value in enumerate(parsed[1])}
            await self._mark_healthy("arm-sdk-joints")

    async def _mark_healthy(self, source: str) -> None:
        self.last_data_at[source] = time.monotonic()
        if source not in self.healthy_sources:
            self.healthy_sources.add(source)
            await self._log(source, "INFO", "SDK 采集正常")

    @staticmethod
    def parse_tuple_line(line: str) -> tuple[str, tuple] | None:
        match = TUPLE_PATTERN.match(line)
        if not match:
            return None
        try:
            values = ast.literal_eval(match.group(2))
        except (SyntaxError, ValueError):
            return None
        return (match.group(1), values) if isinstance(values, tuple) else None

    async def _log(self, source: str, level: str, message: str) -> None:
        self.sequence += 1
        LOGGER.log(logging.ERROR if level == "ERROR" else logging.INFO, "%s: %s", source, message)
        await self.emit({
            "type": "log",
            "payload": {
                "timestamp": datetime.now(timezone.utc).isoformat(), "source": source,
                "level": level, "sequence": self.sequence, "message": message,
            },
        })
