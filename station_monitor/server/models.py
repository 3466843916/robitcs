from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, IPvAnyAddress, SecretStr, field_validator


class ProcessState(StrEnum):
    UNKNOWN = "unknown"
    INACTIVE = "inactive"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    ip: IPvAnyAddress
    ros_domain_id: int = Field(default=0, ge=0, le=232)
    joint_topic: str = Field(default="/joint_states", min_length=1, max_length=200)
    temperature_topics: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=500)
    acquisition_project_id: int | None = Field(default=None, ge=1)


class OnboardRequest(StationCreate):
    username: str = "root"
    password: SecretStr
    ssh_port: int = Field(default=22, ge=1, le=65535)
    accept_host_key: bool = False


class StationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    ip: IPvAnyAddress | None = None
    ssh_username: str | None = Field(default=None, min_length=1, max_length=100)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ros_domain_id: int | None = Field(default=None, ge=0, le=232)
    joint_topic: str | None = Field(default=None, min_length=1, max_length=200)
    temperature_topics: list[str] | None = None
    notes: str | None = Field(default=None, max_length=500)
    acquisition_project_id: int | None = Field(default=None, ge=1)


class ReconnectRequest(BaseModel):
    username: str = Field(default="root", min_length=1, max_length=100)
    password: SecretStr
    ssh_port: int = Field(default=22, ge=1, le=65535)
    accept_host_key: bool = True


class Station(BaseModel):
    id: str
    name: str
    ip: str
    online: bool = False
    deployment_status: str = "registered"
    last_heartbeat: datetime | None = None
    clock_skew_seconds: float | None = None
    robot_state: ProcessState = ProcessState.UNKNOWN
    collection_state: ProcessState = ProcessState.UNKNOWN
    cpu_total: float | None = None
    cpu_agent: float | None = None
    cpu_robot: float | None = None
    cpu_collection: float | None = None
    cpu_per_core: list[float] = Field(default_factory=list)
    joints: dict[str, float] = Field(default_factory=dict)
    temperatures: dict[str, float] = Field(default_factory=dict)
    active_alarm_count: int = 0
    acquisition_project_id: int | None = None


class CommandRequest(BaseModel):
    target: Literal["robot", "collection", "collection_service", "all", "robot_zero", "state_reset", "shell"]
    action: Literal["start", "stop", "restart", "run", "terminate"]
    command: str | None = Field(default=None, max_length=2000)


class BatchCommandRequest(CommandRequest):
    station_ids: list[str] = Field(min_length=1, max_length=5)


class CommandJob(BaseModel):
    id: str
    station_id: str
    target: str
    action: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    result: str | None = None


class TelemetryPayload(BaseModel):
    timestamp: datetime
    cpu_total: float = Field(ge=0, le=100)
    cpu_agent: float = Field(default=0, ge=0, le=100)
    cpu_robot: float = Field(default=0, ge=0, le=100)
    cpu_collection: float = Field(default=0, ge=0, le=100)
    cpu_per_core: list[float] = Field(default_factory=list)
    robot_state: ProcessState
    collection_state: ProcessState
    joints: dict[str, float] = Field(default_factory=dict)
    temperatures: dict[str, float] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr


class AlarmDeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class LogDeleteRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class LogPayload(BaseModel):
    timestamp: datetime
    source: str = Field(min_length=1, max_length=100)
    level: str = "INFO"
    sequence: int = Field(ge=0)
    message: str = Field(max_length=100_000)

    @field_validator("level")
    @classmethod
    def normalize_level(cls, value: str) -> str:
        value = value.upper()
        return value if value in {"DEBUG", "INFO", "WARNING", "ERROR", "FATAL"} else "INFO"


class AgentEnvelope(BaseModel):
    type: Literal["register", "heartbeat", "telemetry", "log", "command_result"]
    station_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None
