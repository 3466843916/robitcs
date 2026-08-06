import argparse
import asyncio
import json
import math
import random
import urllib.request
from datetime import UTC, datetime

import websockets


def ensure_station(http_url: str, number: int) -> str:
    with urllib.request.urlopen(f"{http_url}/api/stations") as response:
        stations = json.load(response)
    name = f"工站 {number}"
    for station in stations:
        if station["name"] == name:
            return station["id"]
    request = urllib.request.Request(
        f"{http_url}/api/stations", method="POST",
        data=json.dumps({"name": name, "ip": f"192.168.10.{100 + number}"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)["id"]


async def station(number: int, station_id: str, ws_url: str, secret: str) -> None:
    state = {"robot": "running" if number < 4 else "inactive", "collection": "running" if number < 3 else "inactive"}
    sequence = 0
    async with websockets.connect(ws_url) as socket:
        await socket.send(json.dumps({"type": "register", "station_id": station_id, "secret": secret, "payload": {}}))
        await socket.recv()

        async def receiver() -> None:
            async for raw in socket:
                msg = json.loads(raw)
                if msg.get("type") != "command":
                    continue
                if msg["target"] == "shell":
                    text = f"$ {msg.get('command', '')}\n模拟工站命令执行成功"
                    await socket.send(json.dumps({"type": "command_result", "station_id": station_id, "payload": {"job_id": msg["job_id"], "success": True, "message": text}}, ensure_ascii=False))
                    continue
                if msg["target"] in {"robot_zero", "state_reset"}:
                    await socket.send(json.dumps({"type": "command_result", "station_id": station_id, "payload": {"job_id": msg["job_id"], "success": True, "message": "模拟操作执行成功"}}, ensure_ascii=False))
                    continue
                if msg["target"] == "collection_service":
                    state["collection"] = "inactive"
                    await socket.send(json.dumps({"type": "command_result", "station_id": station_id, "payload": {"job_id": msg["job_id"], "success": True, "message": "数据采集程序已停止"}}, ensure_ascii=False))
                    continue
                targets = ["robot", "collection"] if msg["target"] == "all" else [msg["target"]]
                for target in targets:
                    state[target] = "inactive" if msg["action"] == "stop" else "running"
                await socket.send(json.dumps({"type": "command_result", "station_id": station_id, "payload": {"job_id": msg["job_id"], "success": True, "message": "模拟命令执行成功"}}, ensure_ascii=False))

        async def sender() -> None:
            nonlocal sequence
            tick = 0
            while True:
                now = datetime.now(UTC).isoformat()
                payload = {
                    "timestamp": now,
                    "cpu_total": 18 + number * 5 + random.random() * 9,
                    "cpu_agent": 1.2,
                    "cpu_robot": 7 + random.random() * 3,
                    "cpu_collection": 12 + random.random() * 4,
                    "cpu_per_core": [12 + number * 3 + random.random() * 20 for _ in range(8)],
                    "robot_state": state["robot"],
                    "collection_state": state["collection"],
                    "joints": {f"joint_{j + 1}": math.sin(tick / 15 + j) * (0.5 + j * 0.06) for j in range(6)},
                    "temperatures": {**{f"joint_{j + 1}": 39 + number * 1.2 + j * 0.7 + random.random() for j in range(6)}, "eef_motor_1": 38 + number + random.random()},
                }
                await socket.send(json.dumps({"type": "telemetry", "station_id": station_id, "payload": payload}))
                if tick % 3 == 0:
                    sequence += 1
                    log = {"timestamp": now, "source": "collection", "level": "INFO", "sequence": sequence, "message": f"采集帧 #{tick * 10}，相机与机械臂状态正常"}
                    await socket.send(json.dumps({"type": "log", "station_id": station_id, "payload": log}, ensure_ascii=False))
                if tick and tick % 90 == 0 and number == 4:
                    sequence += 1
                    log = {"timestamp": now, "source": "robot", "level": "ERROR", "sequence": sequence, "message": "模拟告警：关节通信超时"}
                    await socket.send(json.dumps({"type": "log", "station_id": station_id, "payload": log}, ensure_ascii=False))
                tick += 1
                await asyncio.sleep(1)

        await asyncio.gather(receiver(), sender())


async def main(count: int, http_url: str, secret: str) -> None:
    ws_url = http_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/agent"
    ids = await asyncio.gather(*(asyncio.to_thread(ensure_station, http_url, i) for i in range(1, count + 1)))
    await asyncio.gather(*(station(i, station_id, ws_url, secret) for i, station_id in enumerate(ids, 1)))


def run() -> None:
    parser = argparse.ArgumentParser(description="Run simulated AIRBOT stations")
    parser.add_argument("--count", type=int, default=5, choices=range(1, 6))
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--secret", default="change-me-before-production")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.server, args.secret))


if __name__ == "__main__":
    run()
