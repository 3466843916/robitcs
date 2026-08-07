import argparse
import asyncio
import json
import logging
import os
import ssl
from contextlib import suppress
from datetime import datetime, timezone

import psutil
import websockets

from .config import AgentConfig
from .log_stream import LogStreamer
from .process_control import ProcessController
from .ros_collector import RosCollector
from .sdk_collector import SdkCollector


LOGGER = logging.getLogger("station-monitor-agent")


class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.controller = ProcessController(
            robot_unit=config.robot_unit,
            collection_unit=config.collection_unit,
            robot_zero_unit=config.robot_zero_unit,
            state_reset_unit=config.state_reset_unit,
            station_ip=config.station_ip,
            task_open_command=config.task_open_command,
            task_close_command=config.task_close_command,
            robot_zero_command=config.robot_zero_command,
            state_reset_command=config.state_reset_command,
        )
        self.ros = RosCollector(config.joint_topic, config.temperature_topics, config.ros_domain_id)
        self.outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)
        self.sdk = SdkCollector(config.sdk_motor_command, config.sdk_joint_command, self.emit, config.sdk_eef_motor_command)

    async def emit(self, item: dict) -> None:
        item["station_id"] = self.config.station_id
        try:
            self.outbox.put_nowait(item)
        except asyncio.QueueFull:
            if item.get("type") != "log":
                await self.outbox.put(item)

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.config.server_url.startswith("wss://"):
            return None
        context = ssl.create_default_context(cafile=self.config.ca_cert)
        if self.config.client_cert and self.config.client_key:
            context.load_cert_chain(self.config.client_cert, self.config.client_key)
        return context

    async def run(self) -> None:
        self.ros.start()
        asyncio.create_task(self.sdk.run())
        delay = 1
        while True:
            try:
                async with websockets.connect(
                    self.config.server_url, ssl=self.ssl_context(), ping_interval=10, ping_timeout=10,
                    max_size=2 * 1024 * 1024,
                ) as socket:
                    await socket.send(json.dumps({
                        "type": "register", "station_id": self.config.station_id,
                        "secret": self.config.secret, "payload": {},
                    }))
                    reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                    if reply.get("type") != "registered":
                        raise RuntimeError("中心端拒绝注册")
                    LOGGER.info("connected to central server")
                    delay = 1
                    await self._session(socket)
            except Exception as exc:
                LOGGER.warning("connection lost: %s; retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _session(self, socket) -> None:
        streamer = LogStreamer(self.emit)
        tasks = [
            asyncio.create_task(self._sender(socket)),
            asyncio.create_task(self._receiver(socket)),
            asyncio.create_task(self._telemetry()),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(streamer.follow_files([*self.config.log_paths, "/var/log/airbot/task-actions.log"])),
            asyncio.create_task(streamer.follow_journal(self.config.collection_unit, "collection.log")),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()

    async def _sender(self, socket) -> None:
        while True:
            await socket.send(json.dumps(await self.outbox.get(), ensure_ascii=False))

    async def _receiver(self, socket) -> None:
        async for raw in socket:
            message = json.loads(raw)
            if message.get("type") == "command":
                asyncio.create_task(self._command(message))

    async def _command(self, message: dict) -> None:
        try:
            if message["target"] == "shell":
                result = await self.controller.run_shell(str(message.get("command", "")))
            else:
                result = await self.controller.execute(message["target"], message["action"])
            success = True
        except Exception as exc:
            result, success = str(exc), False
        await self.emit({
            "type": "command_result",
            "payload": {"job_id": message.get("job_id"), "success": success, "message": result},
        })

    async def _heartbeat(self) -> None:
        while True:
            await self.emit({"type": "heartbeat", "payload": {"timestamp": datetime.now(timezone.utc).isoformat()}})
            await asyncio.sleep(2)

    async def _telemetry(self) -> None:
        psutil.cpu_percent(None)
        while True:
            sdk_joints, sdk_temperatures = self.sdk.snapshot()
            ros_joints, ros_temperatures = self.ros.snapshot()
            joints = sdk_joints or ros_joints
            temperatures = sdk_temperatures or ros_temperatures
            robot_state = await self.controller.state("robot")
            if robot_state == "running" and not self.sdk.arm_data_fresh():
                robot_state = "failed"
            await self.emit({
                "type": "telemetry",
                "payload": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "cpu_total": psutil.cpu_percent(None),
                    "cpu_per_core": psutil.cpu_percent(None, percpu=True),
                    "cpu_agent": psutil.Process(os.getpid()).cpu_percent(None),
                    "cpu_robot": await self._process_cpu("robot"),
                    "cpu_collection": await self._process_cpu("collection"),
                    "robot_state": robot_state,
                    "collection_state": await self.controller.state("collection"),
                    "joints": joints, "temperatures": temperatures,
                },
            })
            await asyncio.sleep(1)

    async def _process_cpu(self, target: str) -> float:
        pid = await self.controller.pid(target)
        if not pid:
            return 0.0
        try:
            return min(psutil.Process(pid).cpu_percent(None), 100.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.getenv("STATION_MONITOR_AGENT_CONFIG", "/etc/station-monitor/agent.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Agent(AgentConfig.load(args.config)).run())


if __name__ == "__main__":
    run()
