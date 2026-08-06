from datetime import UTC, date, datetime, timedelta

import pytest

from station_monitor.agent.process_control import ProcessController
from station_monitor.agent.log_stream import LogStreamer
from station_monitor.agent.sdk_collector import SdkCollector
from station_monitor.server.alerts import AlertService
from station_monitor.server.config import Settings
from station_monitor.server.database import Database
from station_monitor.server.log_store import LogStore
from station_monitor.server.models import LogPayload, Severity
from station_monitor.server.onboarding import task_commands_for_station


def test_log_level_is_normalized():
    item = LogPayload(timestamp=datetime.now(UTC), source="robot", level="error", sequence=1, message="failed")
    assert item.level == "ERROR"


def test_unknown_log_level_becomes_info():
    item = LogPayload(timestamp=datetime.now(UTC), source="robot", level="trace", sequence=1, message="ok")
    assert item.level == "INFO"


@pytest.mark.parametrize(
    ("ip", "executable", "station", "category"),
    [
        ("192.168.31.13", "iox2-cabinet-hinge-door-drawer", "hinge_door_drawer", "drawer"),
        ("192.168.31.41", "iox2-cabinet-hinge-door-down", "hinge_door_down", "door_01"),
        ("192.168.31.34", "iox2-cabinet-hinge-door-up", "hinge_door_up", "door_01"),
        ("192.168.31.100", "iox2-cabinet-hinge-door-left", "hinge_door_left", "door_01"),
        ("192.168.31.178", "iox2-cabinet-hinge-door-right", "hinge_door_right", "door_01"),
    ],
)
def test_station_task_commands(ip, executable, station, category):
    open_command, close_command = task_commands_for_station(ip, "fallback-open", "fallback-close")
    assert open_command.startswith(f"{executable} station {station} task open category {category}")
    assert close_command.startswith(f"{executable} station {station} task close category {category}")
    assert f"--x5-host {ip}" in open_command
    assert "--use-station-fsm" in close_command


@pytest.mark.asyncio
async def test_process_controller_rejects_arbitrary_shell():
    controller = ProcessController("robot.service", "collection.service")
    with pytest.raises(ValueError, match="白名单"):
        await controller.execute("robot; rm -rf /", "start")


@pytest.mark.asyncio
async def test_recoverable_cartesian_fallback_is_warning():
    messages = []

    async def emit(item):
        messages.append(item)

    streamer = LogStreamer(emit)
    await streamer._line("task-actions.log", "ERROR CallCartesianPlan failed: Joint velocity violates limits")
    assert messages[0]["payload"]["level"] == "WARNING"
    assert "可恢复" in messages[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_sdk_collector_parses_joint_temperature_and_motor_errors():
    messages = []

    async def emit(item):
        messages.append(item)

    collector = SdkCollector("motor", "joint", emit)
    await collector._parse_motor_line("motor_temperatures (33.0, 36.0, 37.0, 41.0, 37.0, 35.0, 38.0)")
    await collector._parse_motor_line("error_ids (0, 0, 4, 0, 0, 0, 0)")
    await collector._parse_eef_line("motor_temperatures (45.5,)")
    await collector._parse_joint_line("joint_pos (-0.0315, -0.4915, -0.0061, -0.0874, 1.4443, 0.7256, 0.0435)")

    joints, temperatures = collector.snapshot()
    assert joints["joint_7"] == pytest.approx(0.0435)
    assert collector.arm_data_fresh()
    assert temperatures == {
        "motor_1": 33.0, "motor_2": 36.0, "motor_3": 37.0, "motor_4": 41.0,
        "motor_5": 37.0, "motor_6": 35.0, "motor_7": 38.0,
        "eef_motor_1": 45.5,
    }
    errors = [message for message in messages if message["payload"]["level"] == "ERROR"]
    assert "motor_3" in errors[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_log_store_batch_delete(tmp_path):
    database = Database(tmp_path / "monitor.db")
    await database.initialize()
    store = LogStore(tmp_path / "logs", database)
    item = LogPayload(timestamp=datetime.now(UTC), source="arm_app.log", level="ERROR", sequence=9, message="failed")
    path = await store.append("station-1", item)

    assert path.exists()
    assert await store.delete_files([str(path), str(path)]) == 1
    assert not path.exists()
    assert (await store.query_page("station-1", None, 1, 50))["total"] == 0
    assert await store.query_errors("station-1") == []


@pytest.mark.asyncio
async def test_log_store_only_queries_and_clears_selected_day(tmp_path):
    database = Database(tmp_path / "monitor.db")
    await database.initialize()
    store = LogStore(tmp_path / "logs", database)
    today = date.today()
    for sequence, log_date in enumerate((today - timedelta(days=1), today), 1):
        await store.append("station-1", LogPayload(
            timestamp=datetime.combine(log_date, datetime.min.time(), UTC), source="arm_app.log",
            level="ERROR", sequence=sequence, message=str(log_date),
        ))
    page = await store.query_page("station-1", None, 1, 50, today)
    assert page["total"] == 1
    assert page["items"][0]["message"] == str(today)
    await store.append("station-1", LogPayload(
        timestamp=datetime.combine(today, datetime.min.time(), UTC), source="collection.log",
        level="ERROR", sequence=3, message="collection failed",
    ))
    robot_page = await store.query_page("station-1", None, 1, 50, today, "robot")
    collection_page = await store.query_page("station-1", None, 1, 50, today, "collection")
    assert robot_page["total"] == 1
    assert collection_page["total"] == 1
    assert collection_page["items"][0]["source"] == "collection.log"
    assert (await store.clear_day(today, "station-1", "robot"))["deleted"] == 1
    assert (await store.query_page("station-1", None, 1, 50, today))["total"] == 1
    assert (await store.clear_day(today, "station-1", "collection"))["deleted"] == 1
    assert (await store.query_page("station-1", None, 1, 50, today))["total"] == 0
    assert (await store.query_page("station-1", None, 1, 50, today - timedelta(days=1)))["total"] == 1


@pytest.mark.asyncio
async def test_alarm_batch_delete_removes_database_rows(tmp_path):
    database = Database(tmp_path / "monitor.db")
    await database.initialize()
    await database.execute(
        "INSERT INTO stations(id,name,ip,created_at) VALUES(?,?,?,?)",
        ("station-1", "工站 1", "192.0.2.1", datetime.now(UTC).isoformat()),
    )
    alerts = AlertService(database, Settings(data_dir=tmp_path))
    first = await alerts.raise_alarm("station-1", "robot", Severity.CRITICAL, "机械臂异常")
    second = await alerts.raise_alarm("station-1", "collection", Severity.WARNING, "数据采集异常")

    assert await alerts.delete_many([first["id"], second["id"], first["id"]]) == 2
    assert await database.fetch_all("SELECT * FROM alarms") == []


@pytest.mark.asyncio
async def test_collection_open_ensures_rollio_before_task():
    events = []

    class Controller(ProcessController):
        async def state(self, target):
            return "running"

        async def _ensure_collection(self):
            events.append("ensure")

        async def _launch_task(self, command):
            events.append(command)
            return 42

    controller = Controller("robot.service", "collection.service")
    result = await controller.execute("collection", "start")
    assert events == ["ensure", controller.task_open_command]
    assert "PID 42" in result


@pytest.mark.asyncio
async def test_collection_service_stop_terminates_rollio_without_task():
    events = []

    class Controller(ProcessController):
        async def _systemctl(self, action, unit):
            events.append((action, unit))

        async def _terminate_processes(self, pattern):
            events.append(("terminate", pattern))

        async def _launch_task(self, command):
            raise AssertionError("停止采集服务不应启动开关门任务")

    controller = Controller("robot.service", "collection.service")
    result = await controller.execute("collection_service", "stop")
    assert events == [
        ("stop", "collection.service"),
        ("terminate", controller.collection_process),
    ]
    assert result == "数据采集程序已停止"


@pytest.mark.asyncio
async def test_robot_zero_substitutes_station_ip():
    commands = []

    class Controller(ProcessController):
        async def state(self, target):
            return "running"

        async def _run_fixed_command(self, command, timeout):
            commands.append(command)
            return "ok"

    controller = Controller("robot.service", "collection.service", station_ip="192.168.31.178")
    await controller.execute("robot_zero", "start")
    assert "--host 192.168.31.178 --port 50071" in commands[0]
