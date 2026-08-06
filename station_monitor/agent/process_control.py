import asyncio
import os
import shlex
import signal
from dataclasses import dataclass


ALLOWED_ACTIONS = {"start", "stop", "restart", "terminate"}
ALLOWED_TARGETS = {"robot", "collection", "collection_service", "all", "robot_zero", "state_reset"}


@dataclass(slots=True)
class ProcessController:
    robot_unit: str
    collection_unit: str
    robot_zero_unit: str = "airbot-zero.service"
    state_reset_unit: str = "airbot-state-reset.service"
    robot_process: str = "[a]rm[-_]app"
    collection_process: str = "[r]ollio[[:space:]]+collect.*config-cart-3-armmes-g2t-observer[.]toml"
    station_ip: str = ""
    task_open_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task open category drawer --run --auto --repeat-count 0 --recording-enabled"
    task_close_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task close category drawer --run --auto --repeat-count 0 --recording-enabled"
    robot_zero_command: str = "arm-p7-sdk examples run airbot_example_return_zero --host {ip} --port 50071"
    state_reset_command: str = "curl -s -X POST http://127.0.0.1:9090/api/fsm/command -H 'Content-Type: application/json' -d '{\"command\":\"abort\"}'"

    async def state(self, target: str) -> str:
        if target == "robot":
            return "running" if await self._pids_for_process(self.robot_process) else "inactive"
        if target == "collection":
            return "running" if await self._pids_for_process(self.collection_process) else "inactive"
        unit = self._unit(target)
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        value = stdout.decode().strip()
        return {"active": "running", "activating": "starting", "deactivating": "stopping", "failed": "failed"}.get(value, "inactive")

    async def execute(self, target: str, action: str) -> str:
        if target not in ALLOWED_TARGETS or action not in ALLOWED_ACTIONS:
            raise ValueError("命令不在白名单中")
        if target == "collection" and action == "terminate":
            await self._systemctl("stop", self.collection_unit)
            await self._terminate_processes(self.collection_process)
            return "数据采集程序已停止"
        if target in {"robot_zero", "state_reset"}:
            if action != "start":
                raise ValueError("归零和状态机复位只允许执行")
            if await self.state("robot") != "running":
                raise RuntimeError("机械臂服务未运行")
            if target == "robot_zero":
                if not self.station_ip:
                    raise RuntimeError("工站 IP 未配置")
                output = await self._run_fixed_command(self.robot_zero_command.format(ip=self.station_ip), 120)
                return f"机械臂归零命令完成\n{output}"
            output = await self._run_fixed_command(self.state_reset_command, 30)
            return f"状态机复位命令完成\n{output}"
        if target == "all":
            return await self._all(action)
        if target == "collection_service":
            if action != "stop":
                raise ValueError("采集服务只允许停止")
            await self._systemctl("stop", self.collection_unit)
            await self._terminate_processes(self.collection_process)
            return "数据采集程序已停止"
        if target == "collection":
            if await self.state("robot") != "running":
                raise RuntimeError("机械臂程序未运行")
            if action == "restart":
                await self._systemctl("restart", self.collection_unit)
                await self._wait_running("collection", 20)
                return "rollio collect 重启完成"
            await self._ensure_collection()
            command = self.task_open_command if action == "start" else self.task_close_command
            label = "开" if action == "start" else "关"
            pid = await self._launch_task(command)
            return f"数采{label}任务已执行，PID {pid}"
        if target == "robot" and action == "stop" and await self.state("collection") == "running":
            raise RuntimeError("数采正在运行，禁止单独停止机械臂")
        await self._systemctl(action, self._unit(target))
        if target == "robot" and action in {"start", "restart"}:
            await self._wait_running("robot", 20)
        if action == "stop" and target in {"robot", "collection"}:
            await self._terminate_processes(self.robot_process if target == "robot" else self.collection_process)
        return f"{target} {action} 完成"

    async def run_shell(self, command: str) -> str:
        if not command.strip():
            raise ValueError("命令不能为空")
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError("命令执行超过 30 秒，已终止")
        output = (stdout + stderr).decode(errors="replace")[:65536]
        if proc.returncode:
            raise RuntimeError(f"退出码 {proc.returncode}\n{output}")
        return output or "命令执行成功，无输出"

    async def _all(self, action: str) -> str:
        if action == "stop":
            await self._systemctl("stop", self.collection_unit)
            await self._terminate_processes(self.collection_process)
            await self._systemctl("stop", self.robot_unit)
            await self._terminate_processes(self.robot_process)
        elif action == "start":
            await self._ensure_robot()
            await self._ensure_collection()
        else:
            await self._all("stop")
            await self._all("start")
        return f"all {action} 完成"

    async def _wait_running(self, target: str, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        running_since: float | None = None
        while loop.time() < deadline:
            if await self.state(target) == "running":
                running_since = running_since or loop.time()
                if loop.time() - running_since >= 2:
                    return
            else:
                running_since = None
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{target} 未能稳定运行，启动超时")

    async def _systemctl(self, action: str, unit: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", action, unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode:
            raise RuntimeError((stderr or stdout).decode(errors="replace").strip())

    async def _ensure_collection(self) -> None:
        if await self.state("collection") == "running":
            return
        await self._systemctl("restart", self.collection_unit)
        await self._wait_running("collection", 20)

    async def _ensure_robot(self) -> None:
        if await self.state("robot") == "running":
            return
        await self._systemctl("restart", self.robot_unit)
        await self._wait_running("robot", 20)

    async def _terminate_processes(self, pattern: str) -> None:
        pids = await self._pids_for_process(pattern)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        if not pids:
            return
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if not await self._pids_for_process(pattern):
                return
            await asyncio.sleep(0.25)
        for pid in await self._pids_for_process(pattern):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _launch_task(self, command: str) -> int:
        executable = shlex.split(command)[0]
        check = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", f"command -v {shlex.quote(executable)}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await check.wait():
            raise RuntimeError(f"未找到任务命令：{executable}")
        wrapped = (
            f"nohup /bin/bash -lc {shlex.quote(command)} "
            ">> /var/log/airbot/task-actions.log 2>&1 < /dev/null & echo $!"
        )
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", wrapped,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode:
            raise RuntimeError((stderr or stdout).decode(errors="replace").strip())
        try:
            return int(stdout.decode().strip())
        except ValueError as exc:
            raise RuntimeError("任务进程启动后未返回 PID") from exc

    async def _run_fixed_command(self, command: str, timeout: float) -> str:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"命令执行超过 {timeout:.0f} 秒，已终止")
        output = (stdout + stderr).decode(errors="replace").strip()
        if proc.returncode:
            raise RuntimeError(f"退出码 {proc.returncode}\n{output}")
        return output or "命令执行成功，无输出"

    def _unit(self, target: str) -> str:
        if target == "robot":
            return self.robot_unit
        if target in {"collection", "collection_service"}:
            return self.collection_unit
        if target == "robot_zero":
            return self.robot_zero_unit
        if target == "state_reset":
            return self.state_reset_unit
        raise ValueError("未知目标")

    async def pid(self, target: str) -> int | None:
        if target == "robot":
            pids = await self._pids_for_process(self.robot_process)
            return pids[0] if pids else None
        if target == "collection":
            pids = await self._pids_for_process(self.collection_process)
            return pids[0] if pids else None
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", "--property=MainPID", "--value", self._unit(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        try:
            pid = int(stdout.decode().strip())
            return pid or None
        except ValueError:
            return None

    async def _pids_for_process(self, pattern: str) -> list[int]:
        """Use pgrep -f (the safe equivalent of `ps aux | grep pattern`)."""
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", "--", pattern,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return [int(value) for value in stdout.decode().split() if value.isdigit()]
