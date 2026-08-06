from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", env_file=".env")

    host: str = "127.0.0.1"
    port: int = 8080
    public_agent_url: str = "ws://127.0.0.1:8080/ws/agent"
    data_dir: Path = Path("data")
    retention_days: int = 30
    station_limit: int = 0
    heartbeat_timeout_seconds: int = 10
    agent_shared_secret: SecretStr = SecretStr("change-me-before-production")
    allow_insecure_agents: bool = True
    proxy_client_verify_header: str = "x-ssl-client-verify"
    timezone: str = "Asia/Shanghai"
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("040712")
    acquisition_base_url: str = "http://192.168.215.158"
    acquisition_username: str = "admin"
    acquisition_password: SecretStr = SecretStr("123456")

    robot_unit: str = "airbot-robot.service"
    collection_unit: str = "airbot-collection.service"
    robot_zero_unit: str = "airbot-zero.service"
    state_reset_unit: str = "airbot-state-reset.service"
    robot_command: str = "/opt/arm_app/bin/arm_app"
    collection_command: str = "rollio collect --config /userdata/rollio_config/config-cart-3-armmes-g2t-observer.toml"
    sdk_motor_command: str = "arm-p7-sdk examples run airbot_example_get_arm_motor_states"
    sdk_joint_command: str = "arm-p7-sdk examples run airbot_example_get_arm_joint_states"
    sdk_eef_motor_command: str = "arm-p7-sdk examples run airbot_example_get_eef_motor_states"
    robot_zero_command: str = "arm-p7-sdk examples run airbot_example_return_zero --host {ip} --port 50071"
    state_reset_command: str = "curl -s -X POST http://127.0.0.1:9090/api/fsm/command -H 'Content-Type: application/json' -d '{\"command\":\"abort\"}'"
    task_open_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task open category drawer --run --auto --repeat-count 0 --recording-enabled"
    task_close_command: str = "iox2-cabinet-hinge-door-drawer station hinge_door_drawer task close category drawer --run --auto --repeat-count 0 --recording-enabled"
    command_workdir: str = "/var/log/airbot"
    ros_setup: str = "/opt/ros/humble/setup.bash"
    log_paths: str = "/userdata/storage/arm_app/last/log/arm_app.log"

    cpu_warning_percent: float = 85.0
    cpu_critical_percent: float = 95.0
    temperature_warning_c: float | None = None
    temperature_critical_c: float | None = None

    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    alert_email: str | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


settings = Settings()
