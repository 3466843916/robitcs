import json
import shlex
from pathlib import Path

import asyncssh

from .config import Settings
from .models import OnboardRequest


TASK_PROFILES = {
    "192.168.31.13": ("iox2-cabinet-hinge-door-drawer", "hinge_door_drawer", "drawer"),
    "192.168.31.41": ("iox2-cabinet-hinge-door-down", "hinge_door_down", "door_01"),
    "192.168.31.34": ("iox2-cabinet-hinge-door-up", "hinge_door_up", "door_01"),
    "192.168.31.100": ("iox2-cabinet-hinge-door-left", "hinge_door_left", "door_01"),
    "192.168.31.178": ("iox2-cabinet-hinge-door-right", "hinge_door_right", "door_01"),
}


def task_commands_for_station(ip: str, fallback_open: str, fallback_close: str) -> tuple[str, str]:
    profile = TASK_PROFILES.get(ip)
    if not profile:
        return fallback_open, fallback_close
    executable, station, category = profile
    base = f"{executable} station {station} task {{action}} category {category} --run --auto --repeat-count 0 --x5-host {ip} --use-station-fsm --recording-enabled"
    return base.format(action="open"), base.format(action="close")


class OnboardingError(RuntimeError):
    pass


class Onboarder:
    """Installs the restricted agent via an initial password-authenticated SSH session."""

    def __init__(self, config: Settings, project_root: Path):
        self.config = config
        self.project_root = project_root

    async def check_connection(self, request: OnboardRequest) -> None:
        try:
            async with asyncssh.connect(
                str(request.ip), port=request.ssh_port, username=request.username,
                password=request.password.get_secret_value(), known_hosts=None, login_timeout=10,
            ):
                return
        except (asyncssh.Error, OSError) as exc:
            raise OnboardingError(f"SSH 登录失败：{exc}") from exc

    async def install(self, station_id: str, request: OnboardRequest) -> str:
        known_hosts = None if request.accept_host_key else str(self.config.data_dir / "known_hosts")
        try:
            async with asyncssh.connect(
                str(request.ip),
                port=request.ssh_port,
                username=request.username,
                password=request.password.get_secret_value(),
                known_hosts=known_hosts,
            ) as conn:
                check = await conn.run("test -f /etc/os-release && grep -q '22.04' /etc/os-release", check=False)
                if check.exit_status != 0:
                    raise OnboardingError("工站不是受支持的 Ubuntu 22.04")
                dependencies = await conn.run("python3 -c 'import psutil, websockets'", check=False)
                if dependencies.exit_status != 0:
                    await conn.run(
                        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-psutil python3-websockets",
                        check=True,
                    )
                await self._ensure_sdk_cli(conn)
                await conn.run("install -d -m 0755 /opt/station-monitor/station_monitor/agent /etc/station-monitor /var/log/airbot/logs", check=True)
                async with conn.start_sftp_client() as sftp:
                    local_agent = self.project_root / "station_monitor" / "agent"
                    for source in local_agent.glob("*.py"):
                        await sftp.put(str(source), f"/opt/station-monitor/station_monitor/agent/{source.name}")
                    await sftp.put(str(self.project_root / "station_monitor" / "__init__.py"), "/opt/station-monitor/station_monitor/__init__.py")
                    await sftp.put(str(self.project_root / "deploy" / "station-monitor-agent.service"), "/etc/systemd/system/station-monitor-agent.service")
                task_open, task_close = task_commands_for_station(
                    str(request.ip), self.config.task_open_command, self.config.task_close_command
                )
                agent_config = {
                    "station_id": station_id,
                    "station_ip": str(request.ip),
                    "server_url": self.config.public_agent_url,
                    "secret": self.config.agent_shared_secret.get_secret_value(),
                    "robot_unit": self.config.robot_unit,
                    "collection_unit": self.config.collection_unit,
                    "robot_zero_unit": self.config.robot_zero_unit,
                    "state_reset_unit": self.config.state_reset_unit,
                    "ros_domain_id": request.ros_domain_id,
                    "joint_topic": request.joint_topic,
                    "temperature_topics": request.temperature_topics,
                    "sdk_motor_command": self.config.sdk_motor_command,
                    "sdk_joint_command": self.config.sdk_joint_command,
                    "sdk_eef_motor_command": self.config.sdk_eef_motor_command,
                    "task_open_command": task_open,
                    "task_close_command": task_close,
                    "robot_zero_command": self.config.robot_zero_command,
                    "state_reset_command": self.config.state_reset_command,
                    "log_paths": self.config.log_paths.split(","),
                }
                encoded = shlex.quote(json.dumps(agent_config, ensure_ascii=False))
                await conn.run(f"printf %s {encoded} > /etc/station-monitor/agent.json", check=True)
                await self._install_program_unit(conn, self.config.robot_unit, self.config.robot_command)
                await self._install_program_unit(conn, self.config.collection_unit, self.config.collection_command)
                await conn.run("systemctl daemon-reload && systemctl enable station-monitor-agent && systemctl restart station-monitor-agent", check=True)
                result = await conn.run("systemctl is-active station-monitor-agent", check=True)
                return result.stdout.strip()
        except (asyncssh.Error, OSError) as exc:
            raise OnboardingError(str(exc)) from exc

    async def _ensure_sdk_cli(self, conn: asyncssh.SSHClientConnection) -> None:
        check = await conn.run("command -v arm-p7-sdk", check=False)
        if check.exit_status == 0:
            return
        located = await conn.run(
            "find /opt/iox2-cabinet -type d -path '*/python/arm_p7_sdk' -print -quit",
            check=False,
        )
        package_dir = located.stdout.strip()
        if not package_dir:
            return
        python_path = str(Path(package_dir).parent)
        wrapper = "\n".join([
            "#!/bin/sh",
            f"export PYTHONPATH={shlex.quote(python_path)}${{PYTHONPATH:+:$PYTHONPATH}}",
            "exec /usr/bin/python3 -c 'from arm_p7_sdk.cli import main; main()' \"$@\"",
            "",
        ])
        await conn.run(
            f"printf %s {shlex.quote(wrapper)} > /usr/local/bin/arm-p7-sdk && chmod 0755 /usr/local/bin/arm-p7-sdk",
            check=True,
        )

    async def _install_program_unit(self, conn: asyncssh.SSHClientConnection, unit: str, command: str, one_shot: bool = False) -> None:
        lifecycle = ["RemainAfterExit=no"] if one_shot else ["Restart=always", "RestartSec=3"]
        body = "\n".join(
            [
                "[Unit]",
                "After=network-online.target",
                "Wants=network-online.target",
                "[Service]",
                "Type=oneshot" if one_shot else "Type=simple",
                f"WorkingDirectory={self.config.command_workdir}",
                f"ExecStart=/bin/bash -lc {shlex.quote(f'source {self.config.ros_setup} && exec {command}')}",
                *lifecycle,
                "KillSignal=SIGINT",
                "TimeoutStopSec=20",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )
        await conn.run(f"printf %s {shlex.quote(body)} > /etc/systemd/system/{shlex.quote(unit)}", check=True)
