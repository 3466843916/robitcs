import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AgentConfig:
    station_id: str
    server_url: str
    secret: str
    station_ip: str = ""
    robot_unit: str = "airbot-robot.service"
    collection_unit: str = "airbot-collection.service"
    robot_zero_unit: str = "airbot-zero.service"
    state_reset_unit: str = "airbot-state-reset.service"
    ros_domain_id: int = 0
    joint_topic: str = "/joint_states"
    temperature_topics: list[str] = field(default_factory=list)
    sdk_motor_command: str = "arm-p7-sdk examples run airbot_example_get_arm_motor_states"
    sdk_joint_command: str = "arm-p7-sdk examples run airbot_example_get_arm_joint_states"
    sdk_eef_motor_command: str = "arm-p7-sdk examples run airbot_example_get_eef_motor_states"
    task_open_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task open category drawer --run --auto --repeat-count 0 --recording-enabled"
    task_close_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task close category drawer --run --auto --repeat-count 0 --recording-enabled"
    robot_zero_command: str = "arm-p7-sdk examples run airbot_example_return_zero --host {ip} --port 50071"
    state_reset_command: str = "curl -s -X POST http://127.0.0.1:9090/api/fsm/command -H 'Content-Type: application/json' -d '{\"command\":\"abort\"}'"
    log_paths: list[str] = field(default_factory=list)
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        with Path(path).open(encoding="utf-8") as handle:
            return cls(**json.load(handle))
