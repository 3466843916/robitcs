import asyncio
import base64
import json
import posixpath
import re
import secrets
import shutil
import socket as network_socket
import stat
import urllib.error
import urllib.request
import logging
import shlex
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncssh

from .alerts import AlertService
from .config import Settings, settings
from .connections import ConnectionHub
from .database import Database
from .log_store import LogStore
from .models import (
    AgentEnvelope,
    AlarmDeleteRequest,
    BatchCommandRequest,
    CommandRequest,
    LogPayload,
    LogDeleteRequest,
    LoginRequest,
    OnboardRequest,
    ReconnectRequest,
    Severity,
    StationCreate,
    StationUpdate,
    TelemetryPayload,
)
from .onboarding import Onboarder, OnboardingError


LOGGER = logging.getLogger("station-monitor")
ROOT = Path(__file__).resolve().parents[2]
SESSION_COOKIE = "airbot_session"
SESSIONS: set[str] = set()
INTERNET_TUNNEL_PROCESS: asyncio.subprocess.Process | None = None
INTERNET_TUNNEL_URL: str | None = None
INTERNET_TUNNEL_CHECK_FAILURES = 0


def authenticated(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE, "") in SESSIONS


def local_ip_address() -> str:
    probe = network_socket.socket(network_socket.AF_INET, network_socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return network_socket.gethostbyname(network_socket.gethostname())
    finally:
        probe.close()


def network_proxy_ready() -> bool:
    """Return true when the configured LAN proxy already serves this app."""
    try:
        request = urllib.request.Request("http://127.0.0.1:8088/", headers={"User-Agent": "station-monitor"})
        with urllib.request.urlopen(request, timeout=1) as response:
            body = response.read(8192)
            return response.status < 500 and b"AIRBOT" in body
    except (OSError, urllib.error.URLError):
        return False


class Runtime:
    def __init__(self, config: Settings):
        self.config = config
        self.db = Database(config.database_path)
        self.hub = ConnectionHub()
        self.logs = LogStore(config.logs_dir, self.db)
        self.alerts = AlertService(self.db, config, self._notify_alarm_change)
        self.onboarder = Onboarder(config, ROOT)
        self.tasks: list[asyncio.Task] = []
        self.cpu_high_since: dict[str, datetime] = {}
        self.cpu_critical_since: dict[str, datetime] = {}

    async def _notify_alarm_change(self) -> None:
        await self.hub.broadcast({"type": "alarm_update"})


runtime = Runtime(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.db.initialize()
    runtime.config.logs_dir.mkdir(parents=True, exist_ok=True)
    runtime.tasks = [
        asyncio.create_task(heartbeat_monitor()),
        asyncio.create_task(retention_worker()),
    ]
    yield
    if INTERNET_TUNNEL_PROCESS is not None and INTERNET_TUNNEL_PROCESS.returncode is None:
        INTERNET_TUNNEL_PROCESS.terminate()
        with suppress(ProcessLookupError):
            await INTERNET_TUNNEL_PROCESS.wait()
    for task in runtime.tasks:
        task.cancel()
    for task in runtime.tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="AIRBOT 五工站监控平台", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def require_browser_auth(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/auth/login", "/api/acquisition/auto-login"} and not authenticated(request):
        return Response(content='{"detail":"未登录或会话已过期"}', status_code=401, media_type="application/json")
    return await call_next(request)


@app.post("/api/auth/login", status_code=204)
async def login(item: LoginRequest, response: Response):
    username_ok = secrets.compare_digest(item.username, runtime.config.admin_username)
    password_ok = secrets.compare_digest(item.password.get_secret_value(), runtime.config.admin_password.get_secret_value())
    if not username_ok or not password_ok:
        raise HTTPException(401, "账号或密码错误")
    token = secrets.token_urlsafe(32)
    SESSIONS.add(token)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/", max_age=12 * 3600)


@app.get("/api/auth/me")
async def auth_me():
    return {"authenticated": True}


@app.post("/api/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    SESSIONS.discard(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE)



async def station_rows() -> list[dict]:
    rows = await runtime.db.fetch_all(
        """SELECT s.*, t.received_at, t.payload,
        (SELECT count(*) FROM alarms a WHERE a.station_id=s.id AND a.status='active') active_alarm_count
        FROM stations s LEFT JOIN telemetry_latest t ON t.station_id=s.id ORDER BY s.created_at"""
    )
    result = []
    now = datetime.now(UTC)
    for row in rows:
        payload = json.loads(row.pop("payload") or "{}")
        received_at = datetime.fromisoformat(row["received_at"]) if row.get("received_at") else None
        agent_online = row["id"] in runtime.hub.agents and received_at is not None and (
            now - received_at
        ).total_seconds() <= runtime.config.heartbeat_timeout_seconds
        online = agent_online or bool(row.get("ssh_authenticated") and row.get("ssh_reachable"))
        if not agent_online:
            # Telemetry is only trustworthy while the agent heartbeat is fresh.
            # Do not present a stale last-known process state as currently running.
            payload["robot_state"] = "unknown"
            payload["collection_state"] = "unknown"
        result.append(
            {
                "id": row["id"], "name": row["name"], "ip": row["ip"],
                "deployment_status": row["deployment_status"], "online": online,
                "agent_online": agent_online,
                "deployment_message": row.get("deployment_message"),
                "ssh_username": row.get("ssh_username", "root"),
                "ssh_port": row.get("ssh_port", 22),
                "ssh_reachable": bool(row.get("ssh_reachable")),
                "last_ssh_check": row.get("last_ssh_check"),
                "ros_domain_id": row.get("ros_domain_id", 0),
                "joint_topic": row.get("joint_topic", "/joint_states"),
                "temperature_topics": json.loads(row.get("temperature_topics") or "[]"),
                "notes": row.get("notes", ""),
                "acquisition_project_id": row.get("acquisition_project_id"),
                "last_heartbeat": row.get("received_at"),
                "active_alarm_count": row["active_alarm_count"],
                **payload,
            }
        )
    return result


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(UTC)}


@app.get("/api/stations")
async def list_stations():
    return await station_rows()


@app.post("/api/stations", status_code=201)
async def create_station(item: StationCreate):
    count = await runtime.db.fetch_one("SELECT count(*) AS value FROM stations")
    if runtime.config.station_limit > 0 and count and count["value"] >= runtime.config.station_limit:
        raise HTTPException(409, f"最多只能添加 {runtime.config.station_limit} 台工站")
    station_id = str(uuid4())
    try:
        await runtime.db.execute(
            "INSERT INTO stations(id,name,ip,ros_domain_id,joint_topic,temperature_topics,notes,acquisition_project_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (station_id, item.name, str(item.ip), item.ros_domain_id, item.joint_topic, json.dumps(item.temperature_topics), item.notes, item.acquisition_project_id, datetime.now(UTC).isoformat()),
        )
    except Exception as exc:
        raise HTTPException(409, "IP 已存在") from exc
    await runtime.db.audit("station.create", station_id, {"name": item.name, "ip": str(item.ip)})
    return {"id": station_id, "status": "registered"}


@app.post("/api/stations/onboard", status_code=202)
async def onboard_station(item: OnboardRequest):
    created = await create_station(StationCreate(**item.model_dump(exclude={"username", "password", "ssh_port", "accept_host_key"})))
    station_id = created["id"]
    await runtime.db.execute("UPDATE stations SET ssh_username=?,ssh_port=? WHERE id=?", (item.username, item.ssh_port, station_id))

    async def install() -> None:
        await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("installing", "正在通过 SSH 检查并安装 Agent", station_id))
        await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
        try:
            await runtime.onboarder.check_connection(item)
        except OnboardingError as exc:
            await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=?,ssh_authenticated=0,ssh_reachable=0,last_ssh_check=? WHERE id=?", ("failed", str(exc), datetime.now(UTC).isoformat(), station_id))
            await runtime.db.audit("station.install_failed", station_id, {"error": str(exc)})
        else:
            await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=?,ssh_authenticated=1,ssh_reachable=1,last_ssh_check=? WHERE id=?", ("ssh_connected", "SSH 登录成功，正在安装监控组件", datetime.now(UTC).isoformat(), station_id))
            await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
            try:
                await runtime.onboarder.install(station_id, item)
            except OnboardingError as exc:
                await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("failed", f"SSH 在线，但 Agent 安装失败：{exc}", station_id))
                await runtime.db.audit("station.install_failed", station_id, {"error": str(exc), "ssh_connected": True})
            else:
                await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("installed", "SSH 在线，Agent 安装完成，等待监控心跳", station_id))
                await runtime.db.audit("station.installed", station_id, {})
        await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})

    asyncio.create_task(install())
    return created


@app.delete("/api/stations/{station_id}", status_code=204)
async def delete_station(station_id: str):
    await require_station(station_id)
    socket = runtime.hub.agents.get(station_id)
    if socket:
        await socket.close(code=4002, reason="station deleted")
        await runtime.hub.remove_agent(station_id, socket)
    await runtime.db.execute("DELETE FROM stations WHERE id=?", (station_id,))
    await runtime.db.audit("station.delete", station_id, {})
    await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})


@app.patch("/api/stations/{station_id}")
async def update_station(station_id: str, item: StationUpdate):
    await require_station(station_id)
    values = item.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(422, "没有需要修改的字段")
    if "ip" in values:
        values["ip"] = str(values["ip"])
        values["ssh_authenticated"] = 0
        values["ssh_reachable"] = 0
    if "ssh_username" in values or "ssh_port" in values:
        values["ssh_authenticated"] = 0
        values["ssh_reachable"] = 0
    if "temperature_topics" in values:
        values["temperature_topics"] = json.dumps(values["temperature_topics"], ensure_ascii=False)
    assignments = ",".join(f"{key}=?" for key in values)
    try:
        await runtime.db.execute(f"UPDATE stations SET {assignments} WHERE id=?", tuple(values.values()) + (station_id,))
    except Exception as exc:
        raise HTTPException(409, "IP 已被其他工站使用") from exc
    await runtime.db.audit("station.update", station_id, values)
    await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
    return {"id": station_id, **values}


@app.post("/api/stations/{station_id}/reconnect", status_code=202)
async def reconnect_station(station_id: str, item: ReconnectRequest):
    station = await require_station(station_id)
    request = OnboardRequest(
        name=station["name"], ip=station["ip"], username=item.username,
        password=item.password, ssh_port=item.ssh_port, accept_host_key=item.accept_host_key,
        ros_domain_id=station.get("ros_domain_id", 0), joint_topic=station.get("joint_topic", "/joint_states"),
        temperature_topics=json.loads(station.get("temperature_topics") or "[]"), notes=station.get("notes", ""),
    )
    await runtime.db.execute("UPDATE stations SET ssh_username=?,ssh_port=? WHERE id=?", (item.username, item.ssh_port, station_id))

    async def reconnect() -> None:
        await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("installing", "正在重新验证 SSH 并安装 Agent", station_id))
        await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
        try:
            await runtime.onboarder.check_connection(request)
        except OnboardingError as exc:
            await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=?,ssh_authenticated=0,ssh_reachable=0,last_ssh_check=? WHERE id=?", ("failed", str(exc), datetime.now(UTC).isoformat(), station_id))
        else:
            await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=?,ssh_authenticated=1,ssh_reachable=1,last_ssh_check=? WHERE id=?", ("ssh_connected", "SSH 登录成功，正在安装监控组件", datetime.now(UTC).isoformat(), station_id))
            await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
            try:
                await runtime.onboarder.install(station_id, request)
            except OnboardingError as exc:
                await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("failed", f"SSH 在线，但 Agent 安装失败：{exc}", station_id))
            else:
                await runtime.db.execute("UPDATE stations SET deployment_status=?,deployment_message=? WHERE id=?", ("installed", "SSH 在线，Agent 安装完成，等待监控心跳", station_id))
        await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})

    asyncio.create_task(reconnect())
    return {"id": station_id, "status": "installing"}


async def require_station(station_id: str) -> dict:
    row = await runtime.db.fetch_one("SELECT * FROM stations WHERE id=?", (station_id,))
    if not row:
        raise HTTPException(404, "工站不存在")
    return row


async def create_command(station_id: str, item: CommandRequest) -> dict:
    await require_station(station_id)
    telemetry = runtime.hub.telemetry.get(station_id, {})
    if item.target == "shell" and (item.action != "run" or not item.command or not item.command.strip()):
        raise HTTPException(422, "远程命令不能为空")
    if item.target == "collection" and item.action in {"start", "restart"} and telemetry.get("robot_state") != "running":
        raise HTTPException(409, "机械臂程序未运行，不能启动数采")
    if item.target == "robot" and item.action == "stop" and telemetry.get("collection_state") == "running":
        raise HTTPException(409, "数采正在运行，请使用“全部停止”")
    if item.target in {"robot_zero", "state_reset"} and telemetry.get("robot_state") != "running":
        raise HTTPException(409, "机械臂服务未运行，不能执行该操作")
    job_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    await runtime.db.execute(
        "INSERT INTO command_jobs(id,station_id,target,action,status,created_at) VALUES(?,?,?,?,?,?)",
        (job_id, station_id, item.target, item.action, "queued", now),
    )
    target, action, command = item.target, item.action, item.command
    if item.target == "collection" and item.action == "terminate":
        target, action = "shell", "run"
        command = (
            f"systemctl stop {shlex.quote(runtime.config.collection_unit)}; "
            "pkill -TERM -f 'rollio[[:space:]]+collect.*config-cart-3-armmes-g2t-observer[.]toml' || true"
        )
    sent = await runtime.hub.send_command(
        station_id, {"type": "command", "job_id": job_id, "target": target, "action": action, "command": command}
    )
    if not sent:
        await runtime.db.execute(
            "UPDATE command_jobs SET status='failed',finished_at=?,result=? WHERE id=?",
            (datetime.now(UTC).isoformat(), "工站离线", job_id),
        )
        raise HTTPException(409, "工站离线")
    await runtime.db.execute("UPDATE command_jobs SET status='sent' WHERE id=?", (job_id,))
    await runtime.db.audit("command.send", station_id, {"job_id": job_id, **item.model_dump()})
    return {"id": job_id, "station_id": station_id, "status": "sent"}


@app.post("/api/stations/{station_id}/commands", status_code=202)
async def command_station(station_id: str, item: CommandRequest):
    return await create_command(station_id, item)


@app.post("/api/commands/batch", status_code=202)
async def batch_command(item: BatchCommandRequest):
    results = []
    command = CommandRequest(target=item.target, action=item.action)
    for station_id in item.station_ids:
        try:
            results.append(await create_command(station_id, command))
        except HTTPException as exc:
            results.append({"station_id": station_id, "status": "failed", "detail": exc.detail})
    return results


@app.get("/api/commands")
async def list_commands(limit: int = Query(100, ge=1, le=500)):
    return await runtime.db.fetch_all("SELECT * FROM command_jobs ORDER BY created_at DESC LIMIT ?", (limit,))


@app.get("/api/alarms")
async def list_alarms(status: str | None = None):
    if status:
        return await runtime.db.fetch_all("SELECT * FROM alarms WHERE status=? ORDER BY last_at DESC", (status,))
    return await runtime.db.fetch_all("SELECT * FROM alarms ORDER BY last_at DESC LIMIT 1000")


@app.post("/api/alarms/{alarm_id}/acknowledge", status_code=204)
async def acknowledge_alarm(alarm_id: str):
    await runtime.alerts.acknowledge(alarm_id)
    await runtime.db.audit("alarm.acknowledge", alarm_id, {})


@app.post("/api/alarms/acknowledge-all", status_code=204)
async def acknowledge_all_alarms():
    count = await runtime.alerts.acknowledge_all()
    await runtime.db.audit("alarm.acknowledge_all", None, {"count": count})


async def delete_alarm_batch(item: AlarmDeleteRequest):
    deleted = await runtime.alerts.delete_many(item.ids)
    await runtime.db.audit("alarms.delete_batch", None, {"ids": item.ids, "deleted": deleted})
    return {"deleted": deleted}


@app.post("/api/alarms/delete-batch")
async def delete_alarms_batch(item: AlarmDeleteRequest):
    return await delete_alarm_batch(item)


@app.delete("/api/alarms")
async def delete_alarms(item: AlarmDeleteRequest):
    return await delete_alarm_batch(item)


@app.delete("/api/alarms/{alarm_id}", status_code=204)
async def delete_alarm(alarm_id: str):
    await runtime.alerts.delete(alarm_id)
    await runtime.db.audit("alarm.delete", alarm_id, {})


@app.get("/api/logs/errors")
async def error_logs(station_id: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    return await runtime.logs.query_errors(station_id, limit)


@app.get("/api/logs")
async def paged_logs(
    station_id: str | None = None,
    level: str | None = None,
    log_date: date | None = None,
    source_group: str | None = Query(None, pattern="^(robot|collection)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
):
    return await runtime.logs.query_page(station_id, level, page, page_size, log_date, source_group)


@app.delete("/api/logs/today")
async def clear_today_logs(
    log_date: date, station_id: str | None = None,
    source_group: str | None = Query(None, pattern="^(robot|collection)$"),
):
    result = await runtime.logs.clear_day(log_date, station_id, source_group)
    await runtime.db.audit("logs.clear_day", station_id, {"log_date": log_date.isoformat(), **result})
    return result


@app.get("/api/logs/files")
async def log_files(station_id: str | None = None):
    return await runtime.logs.list_files(station_id)


@app.delete("/api/logs/files")
async def delete_log_files(item: LogDeleteRequest):
    deleted = await runtime.logs.delete_files(item.paths)
    await runtime.db.audit("logs.delete_batch", None, {"paths": item.paths, "deleted": deleted})
    return {"deleted": deleted}


@app.get("/api/logs/download")
async def download_log(path: str):
    candidate = Path(path).resolve()
    root = runtime.config.logs_dir.resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise HTTPException(404, "日志文件不存在")
    return StreamingResponse(
        runtime.logs.stream_file(candidate), media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{candidate.name}"'},
    )


PROJECT_BY_STATION_IP = {
    "192.168.31.13": 184,
    "192.168.31.41": 185,
    "192.168.31.178": 186,
    "192.168.31.34": 187,
    "192.168.31.100": 182,
}


def acquisition_login() -> dict:
    payload = json.dumps({
        "username": runtime.config.acquisition_username,
        "password": runtime.config.acquisition_password.get_secret_value(),
    }).encode()
    request = urllib.request.Request(
        runtime.config.acquisition_base_url.rstrip("/") + "/usermanger/api/users/login",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read())
    if result.get("code") != 200 or not result.get("access_token"):
        raise RuntimeError(result.get("message") or "数采系统登录失败")
    return result



def proxy_acquisition_upstream(path: str, request: Request) -> Response:
    target = f"{runtime.config.acquisition_base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target += "?" + request.url.query
    body = request.scope.get("_body", b"")
    headers = {key: value for key, value in request.headers.items() if key.lower() in {"content-type", "accept", "cookie", "authorization"}}
    req = urllib.request.Request(target, data=body or None, headers=headers, method=request.method)
    with urllib.request.urlopen(req, timeout=20) as upstream:
        content_type = upstream.headers.get("Content-Type", "application/octet-stream")
        return Response(content=upstream.read(), status_code=upstream.status, media_type=content_type.split(";", 1)[0])


async def acquisition_upstream_response(path: str, request: Request) -> Response:
    request.scope["_body"] = await request.body()
    try:
        return await asyncio.to_thread(proxy_acquisition_upstream, path, request)
    except (OSError, urllib.error.URLError) as exc:
        raise HTTPException(502, f"数据采集页面代理失败：{exc}") from exc


@app.api_route("/acquisition/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], response_class=Response)
async def acquisition_proxy(path: str, request: Request):
    """Proxy the browser-facing acquisition app through the monitor origin."""
    return await acquisition_upstream_response(f"acquisition/{path}", request)


@app.api_route("/assets/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], response_class=Response)
async def acquisition_assets_proxy(path: str, request: Request):
    return await acquisition_upstream_response(f"assets/{path}", request)


@app.api_route("/dataserver/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], response_class=Response)
async def acquisition_dataserver_proxy(path: str, request: Request):
    return await acquisition_upstream_response(f"dataserver/{path}", request)


@app.api_route("/usermanger/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], response_class=Response)
async def acquisition_usermanger_proxy(path: str, request: Request):
    return await acquisition_upstream_response(f"usermanger/{path}", request)


@app.api_route("/fonts/{path:path}", methods=["GET"], response_class=Response)
async def acquisition_fonts_proxy(path: str, request: Request):
    return await acquisition_upstream_response(f"fonts/{path}", request)


@app.get("/logo.png", response_class=Response)
async def acquisition_logo_proxy(request: Request):
    return await acquisition_upstream_response("logo.png", request)


@app.get("/aws-sdk.min.js", response_class=Response)
async def acquisition_aws_sdk_proxy(request: Request):
    return await acquisition_upstream_response("aws-sdk.min.js", request)


@app.get("/msgpack5.min.js", response_class=Response)
async def acquisition_msgpack_proxy(request: Request):
    return await acquisition_upstream_response("msgpack5.min.js", request)


@app.get("/api/acquisition/auto-login", response_class=HTMLResponse)
async def acquisition_auto_login(station_id: str):
    station = await runtime.db.fetch_one("SELECT ip,acquisition_project_id FROM stations WHERE id=?", (station_id,))
    if not station:
        raise HTTPException(404, "工站不存在")
    project_id = station.get("acquisition_project_id") or PROJECT_BY_STATION_IP.get(station["ip"])
    if not project_id:
        raise HTTPException(422, "该工站尚未配置数据采集项目 ID")
    try:
        login = await asyncio.to_thread(acquisition_login)
    except (OSError, urllib.error.URLError, RuntimeError, ValueError):
        # Keep the acquisition entry usable when the upstream login service is
        # temporarily unreachable; the browser can still open its login page.
        fallback = f"/acquisition/project?project_id={project_id}"
        return HTMLResponse(f"<!doctype html><meta charset=\"utf-8\"><title>数据采集登录</title><p>正在打开数据采集登录页…</p><script>location.replace({json.dumps(fallback)});</script>")
    values = {
        "myMicroAppToken": login["access_token"],
        "myMicroAppRefreshToken": login.get("refresh_token", ""),
        "refreshTokenTime": str(int(datetime.now(UTC).timestamp() * 1000)),
        "username": login.get("username", runtime.config.acquisition_username),
        "user_id": str(login.get("user_id", "")),
    }
    script_values = json.dumps(values, ensure_ascii=False).replace("</", "<\/")
    target = f"/acquisition/sample?project_id={project_id}"
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>正在进入数据采集</title>
<style>body{{margin:0;background:#141414;color:#d9e2f0;font:14px system-ui;display:grid;place-items:center;min-height:100vh}}</style>
<p>正在自动登录并进入数据采集界面…</p><script>
const values={script_values}; Object.entries(values).forEach(([key,value])=>localStorage.setItem(key,value));
location.replace({json.dumps(target)});
</script>""")


@app.get("/api/network/access")
async def network_access():
    ip = local_ip_address()
    nginx_path = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else None)
    async def _port_open(port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False
    enabled = await _port_open(8088) and await _port_open(8081)
    return {"ip": ip, "url": f"http://{ip}:8088", "acquisition_url": f"http://{ip}:8081", "nginx_available": nginx_path is not None, "enabled": enabled}


@app.post("/api/network/nginx")
async def enable_nginx_access():
    ip = local_ip_address()
    async def _port_open(port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False
    if await _port_open(8088) and await _port_open(8081):
        return {"ip": ip, "url": f"http://{ip}:8088", "acquisition_url": f"http://{ip}:8081", "enabled": True, "already_running": True}
    config = runtime.config.data_dir / "workstation-monitor-nginx.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"""server {{
    listen 8088;
    server_name _;
    client_max_body_size 25m;
    location = /_airbot_login {{
        proxy_pass http://127.0.0.1:8080/api/acquisition/auto-login;
        proxy_set_header Host $host;
    }}
    location / {{
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }}
}}
server {{
    listen 8081;
    server_name _;
    client_max_body_size 25m;
    location = /_airbot_login {{
        proxy_pass http://127.0.0.1:8080/api/acquisition/auto-login;
        proxy_set_header Host $host;
    }}
    location / {{
        proxy_pass {runtime.config.acquisition_base_url.rstrip("/")};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}
""", encoding="utf-8")
    if await asyncio.to_thread(network_proxy_ready):
        await runtime.db.audit("network.nginx_enable", None, {"ip": ip, "config": str(config), "mode": "existing"})
        return {"ip": ip, "url": f"http://{ip}:8088", "acquisition_url": f"http://{ip}:8081", "enabled": True}

    nginx_path = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else None)
    if nginx_path is None:
        install_commands = [
            ("sudo", "-n", "apt-get", "update"),
            ("sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "nginx"),
        ]
        for command in install_commands:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            if process.returncode:
                detail = (stderr or stdout).decode(errors="replace").strip()
                raise HTTPException(503, f"Nginx 自动安装失败：{detail}")
        nginx_path = "/usr/sbin/nginx"
    commands = [
        ("sudo", "-n", "install", "-m", "0644", str(config), "/etc/nginx/conf.d/workstation-monitor.conf"),
        ("sudo", "-n", nginx_path, "-t"),
        ("sudo", "-n", "systemctl", "enable", "--now", "nginx"),
        ("sudo", "-n", "systemctl", "reload", "nginx"),
    ]
    for command in commands:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = (stderr or stdout).decode(errors="replace").strip()
            raise HTTPException(503, f"Nginx 配置未启用：{detail}；配置文件：{config}")
    await runtime.db.audit("network.nginx_enable", None, {"ip": ip, "config": str(config)})
    return {"ip": ip, "url": f"http://{ip}:8088", "acquisition_url": f"http://{ip}:8081", "enabled": True}


@app.post("/api/network/remote")
async def enable_remote_access():
    if not await asyncio.to_thread(network_proxy_ready):
        raise HTTPException(503, "请先开启局域网访问，再开启远程访问")
    tailscale_path = shutil.which("tailscale")
    if tailscale_path is None:
        raise HTTPException(503, "尚未安装 Tailscale。请查看“远程访问开启说明”，安装并登录后重试。")
    process = await asyncio.create_subprocess_exec(tailscale_path, "ip", "-4", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise HTTPException(503, f"Tailscale 尚未连接：{detail or '请先执行 sudo tailscale up'}")
    addresses = [line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()]
    if not addresses:
        raise HTTPException(503, "Tailscale 尚未分配远程网络 IP，请先登录 Tailscale")
    ip = addresses[0]
    await runtime.db.audit("network.remote_enable", None, {"ip": ip, "provider": "tailscale"})
    return {"ip": ip, "url": f"http://{ip}:8088", "enabled": True, "provider": "tailscale"}


async def drain_process_output(process: asyncio.subprocess.Process) -> None:
    if process.stdout is None:
        return
    while await process.stdout.readline():
        pass


async def tunnel_url_alive(url: str) -> bool:
    """Return true if an existing temporary tunnel URL still reaches this app."""
    def _probe() -> bool:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "station-monitor"}, method="HEAD")
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except (OSError, urllib.error.URLError, TimeoutError):
            return False
    return await asyncio.to_thread(_probe)


@app.get("/api/network/internet/status")
async def internet_access_status():
    global INTERNET_TUNNEL_CHECK_FAILURES
    url = INTERNET_TUNNEL_URL
    if not url:
        return {"enabled": False, "stale": False, "url": None}
    # A fresh tunnel can need several seconds for DNS/edge propagation. Require
    # three consecutive failed probes before asking the user to replace it.
    alive = await tunnel_url_alive(url)
    INTERNET_TUNNEL_CHECK_FAILURES = 0 if alive else INTERNET_TUNNEL_CHECK_FAILURES + 1
    stale = not alive and INTERNET_TUNNEL_CHECK_FAILURES >= 3
    return {"enabled": not stale, "stale": stale, "url": url}


@app.post("/api/network/internet")
async def enable_internet_access():
    password = runtime.config.admin_password.get_secret_value()
    global INTERNET_TUNNEL_PROCESS, INTERNET_TUNNEL_URL, INTERNET_TUNNEL_CHECK_FAILURES
    if password == "123456" or len(password) < 12:
        raise HTTPException(503, "为防止工站控制台暴露后被入侵，请先在 .env 中设置至少 12 位的 MONITOR_ADMIN_PASSWORD，并重启服务。")
    if not await asyncio.to_thread(network_proxy_ready):
        raise HTTPException(503, "请先开启局域网访问，再配置互联网访问")
    cloudflared_path = shutil.which("cloudflared")
    if cloudflared_path is None:
        local_path = ROOT / "data" / "tools" / "cloudflared"
        if local_path.is_file():
            cloudflared_path = str(local_path)
    if cloudflared_path is None:
        raise HTTPException(503, "尚未安装 Cloudflare Tunnel。请查看“互联网访问开启说明”，安装并配置后重试。")
    if INTERNET_TUNNEL_PROCESS is not None and INTERNET_TUNNEL_PROCESS.returncode is None and INTERNET_TUNNEL_URL:
        if await tunnel_url_alive(INTERNET_TUNNEL_URL):
            return {"url": INTERNET_TUNNEL_URL, "enabled": True, "provider": "cloudflare-quick-tunnel", "temporary": True}
        INTERNET_TUNNEL_PROCESS.terminate()
        await INTERNET_TUNNEL_PROCESS.wait()
        INTERNET_TUNNEL_PROCESS = None
        INTERNET_TUNNEL_URL = None
    process = await asyncio.create_subprocess_exec(
        cloudflared_path, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8088",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    INTERNET_TUNNEL_PROCESS = process
    if process.stdout is None:
        process.terminate()
        await process.wait()
        INTERNET_TUNNEL_PROCESS = None
        raise HTTPException(503, "Cloudflare Tunnel 启动失败：无法读取进程输出")
    recent_lines: list[str] = []
    for _ in range(30):
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=1)
        except TimeoutError:
            if process.returncode is not None:
                break
            continue
        if not line:
            break
        text = line.decode(errors="replace").strip()
        recent_lines.append(text)
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
        if match and match.group(0) != "https://api.trycloudflare.com":
            INTERNET_TUNNEL_URL = match.group(0)
            INTERNET_TUNNEL_CHECK_FAILURES = 0
            runtime.tasks.append(asyncio.create_task(drain_process_output(process)))
            await runtime.db.audit("network.internet_enable", None, {"url": INTERNET_TUNNEL_URL, "provider": "cloudflare-quick-tunnel"})
            return {"url": INTERNET_TUNNEL_URL, "enabled": True, "provider": "cloudflare-quick-tunnel", "temporary": True}
    if process.returncode is None:
        process.terminate()
        await process.wait()


    INTERNET_TUNNEL_PROCESS = None
    cloudflare_detail = "\n".join(recent_lines[-6:]) or "未获得 Cloudflare 临时地址"
    ssh_path = shutil.which("ssh")
    if ssh_path is None:
        raise HTTPException(503, f"Cloudflare Tunnel 启动失败：{cloudflare_detail}；系统未安装 SSH，无法启用备用隧道")
    ssh_process = await asyncio.create_subprocess_exec(
        ssh_path, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
        "-R", "80:127.0.0.1:8088", "nokey@localhost.run",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    INTERNET_TUNNEL_PROCESS = ssh_process
    if ssh_process.stdout is None:
        ssh_process.terminate(); await ssh_process.wait(); INTERNET_TUNNEL_PROCESS = None
        raise HTTPException(503, "localhost.run 备用隧道启动失败：无法读取进程输出")
    ssh_lines: list[str] = []
    for _ in range(30):
        try:
            line = await asyncio.wait_for(ssh_process.stdout.readline(), timeout=1)
        except TimeoutError:
            if ssh_process.returncode is not None: break
            continue
        if not line: break
        text = line.decode(errors="replace").strip(); ssh_lines.append(text)
        match = re.search(r"https://[a-z0-9-]+\.lhr\.life", text)
        if match:
            INTERNET_TUNNEL_URL = match.group(0)
            INTERNET_TUNNEL_CHECK_FAILURES = 0
            runtime.tasks.append(asyncio.create_task(drain_process_output(ssh_process)))
            await runtime.db.audit("network.internet_enable", None, {"url": INTERNET_TUNNEL_URL, "provider": "localhost.run"})
            return {"url": INTERNET_TUNNEL_URL, "enabled": True, "provider": "localhost.run", "temporary": True}
    if ssh_process.returncode is None:
        ssh_process.terminate(); await ssh_process.wait()
    INTERNET_TUNNEL_PROCESS = None
    ssh_detail = "\n".join(ssh_lines[-6:]) or "未获得 localhost.run 临时地址"
    raise HTTPException(503, f"临时公网隧道启动失败。Cloudflare：{cloudflare_detail}\nlocalhost.run：{ssh_detail}")


@app.websocket("/ws/browser")
async def browser_socket(socket: WebSocket):
    if socket.cookies.get(SESSION_COOKIE, "") not in SESSIONS:
        await socket.close(4401, "authentication required")
        return
    await socket.accept()
    runtime.hub.browsers.add(socket)
    try:
        await socket.send_json({"type": "snapshot", "stations": await station_rows()})
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        runtime.hub.browsers.discard(socket)


@app.websocket("/ws/agent")
async def agent_socket(socket: WebSocket):
    await socket.accept()
    station_id: str | None = None
    try:
        raw = await asyncio.wait_for(socket.receive_json(), timeout=10)
        envelope = AgentEnvelope.model_validate(raw)
        if envelope.type != "register":
            await socket.close(4400, "first message must register")
            return
        verified = socket.headers.get(runtime.config.proxy_client_verify_header, "") == "SUCCESS"
        secret_ok = envelope.secret == runtime.config.agent_shared_secret.get_secret_value()
        if not verified and not (runtime.config.allow_insecure_agents and secret_ok):
            await socket.close(4403, "agent authentication failed")
            return
        if not await runtime.db.fetch_one("SELECT id FROM stations WHERE id=?", (envelope.station_id,)):
            await socket.close(4404, "station not registered")
            return
        station_id = envelope.station_id
        await runtime.hub.register_agent(station_id, socket)
        await runtime.db.execute("UPDATE stations SET deployment_status='connected' WHERE id=?", (station_id,))
        await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})
        await socket.send_json({"type": "registered", "station_id": station_id})
        while True:
            envelope = AgentEnvelope.model_validate(await socket.receive_json())
            if envelope.station_id != station_id:
                continue
            runtime.hub.last_seen[station_id] = datetime.now(UTC)
            if envelope.type == "heartbeat":
                await touch_station(station_id)
            elif envelope.type == "telemetry":
                await handle_telemetry(station_id, TelemetryPayload.model_validate(envelope.payload))
            elif envelope.type == "log":
                await handle_log(station_id, LogPayload.model_validate(envelope.payload))
            elif envelope.type == "command_result":
                await handle_command_result(station_id, envelope.payload)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        LOGGER.exception("agent socket failed for %s", station_id)
    finally:
        if station_id:
            await runtime.hub.remove_agent(station_id, socket)
            await runtime.hub.broadcast({"type": "station_update", "station_id": station_id})


@app.websocket("/ws/terminal/{station_id}")
async def ssh_terminal(socket: WebSocket, station_id: str):
    if socket.cookies.get(SESSION_COOKIE, "") not in SESSIONS:
        await socket.close(4401, "authentication required")
        return
    await socket.accept()
    process = None
    def safe_path(value: object, allow_current: bool = True) -> str:
        path = str(value or "").replace("\\", "/")
        if path.startswith("/") or any(part == ".." for part in path.split("/")):
            raise ValueError("文件操作路径不安全，只允许当前 SSH 用户目录内的相对路径")
        if not allow_current and path in {"", "."}:
            raise ValueError("禁止操作当前目录")
        return path or "."
    try:
        station = await runtime.db.fetch_one("SELECT ip FROM stations WHERE id=?", (station_id,))
        if not station:
            await socket.send_json({"type": "error", "message": "工站不存在"})
            await socket.close(4404)
            return
        auth = await asyncio.wait_for(socket.receive_json(), timeout=30)
        username = str(auth.get("username", "root"))[:100]
        password = str(auth.get("password", ""))
        port = int(auth.get("port", 22))
        if not password or not 1 <= port <= 65535:
            raise ValueError("SSH 凭据不完整")
        async with asyncssh.connect(
            station["ip"], port=port, username=username, password=password, known_hosts=None,
            login_timeout=15,
        ) as connection:
            sftp = await connection.start_sftp_client()
            process = await connection.create_process(
                term_type="xterm-256color",
                term_size=(int(auth.get("cols", 100)), int(auth.get("rows", 30))),
                encoding=None,
            )
            await runtime.db.audit("terminal.open", station_id, {"username": username, "port": port})
            await socket.send_json({"type": "connected"})

            async def to_browser() -> None:
                while data := await process.stdout.read(4096):
                    await socket.send_bytes(data)

            async def to_ssh() -> None:
                while True:
                    message = await socket.receive()
                    if message.get("bytes") is not None:
                        process.stdin.write(message["bytes"])
                    elif message.get("text"):
                        payload = json.loads(message["text"])
                        if payload.get("type") == "input":
                            process.stdin.write(str(payload.get("data", "")).encode())
                        elif payload.get("type") == "resize":
                            process.change_terminal_size(int(payload["cols"]), int(payload["rows"]))
                        elif payload.get("type") == "file_list":
                            path = safe_path(payload.get("path"), True)
                            entries = []
                            async for entry in sftp.scandir(path):
                                permissions = entry.attrs.permissions or 0
                                entries.append({
                                    "name": entry.filename,
                                    "path": posixpath.join(path.rstrip("/") or "/", entry.filename),
                                    "is_dir": stat.S_ISDIR(permissions),
                                    "size": entry.attrs.size or 0,
                                    "mtime": entry.attrs.mtime,
                                })
                            entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
                            await socket.send_json({"type": "file_list", "path": path, "entries": entries})
                        elif payload.get("type") == "file_download":
                            path = safe_path(payload.get("path"), False)
                            attrs = await sftp.stat(path)
                            if (attrs.size or 0) > 20 * 1024 * 1024:
                                raise ValueError("单个下载文件不能超过 20 MB")
                            async with sftp.open(path, "rb") as remote:
                                data = await remote.read()
                            await socket.send_json({
                                "type": "file_download", "name": posixpath.basename(path),
                                "data": base64.b64encode(data).decode(),
                            })
                        elif payload.get("type") == "file_write":
                            path = safe_path(payload.get("path"), False)
                            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
                            if not path or len(data) > 20 * 1024 * 1024:
                                raise ValueError("写入文件无效或超过 20 MB")
                            async with sftp.open(path, "wb") as remote:
                                await remote.write(data)
                            await socket.send_json({"type": "file_written", "path": path})
                        elif payload.get("type") == "file_upload":
                            directory = safe_path(payload.get("path"), True)
                            name = posixpath.basename(str(payload.get("name") or ""))
                            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
                            if not name or len(data) > 20 * 1024 * 1024:
                                raise ValueError("上传文件无效或超过 20 MB")
                            target = posixpath.join(directory, name)
                            async with sftp.open(target, "wb") as remote:
                                await remote.write(data)
                            await socket.send_json({"type": "file_uploaded", "path": directory, "name": name})
                        elif payload.get("type") == "file_delete":
                            path = safe_path(str(payload.get("path") or "").rstrip("/"), False)
                            if path in {"", "."} or path == "/":
                                raise ValueError("禁止删除根目录或当前目录")
                            attrs = await sftp.stat(path)
                            if stat.S_ISDIR(attrs.permissions or 0):
                                await sftp.rmdir(path)
                            else:
                                await sftp.remove(path)
                            await socket.send_json({
                                "type": "file_deleted",
                                "path": posixpath.dirname(path) or ".",
                                "name": posixpath.basename(path),
                            })

            tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_ssh())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with suppress(WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await socket.send_json({"type": "error", "message": f"SSH 连接失败：{exc}"})
    finally:
        if process:
            process.terminate()
        with suppress(Exception):
            await socket.close()


async def touch_station(station_id: str) -> None:
    now = datetime.now(UTC)
    payload = runtime.hub.telemetry.get(station_id, {})
    await runtime.db.execute(
        """INSERT INTO telemetry_latest(station_id,received_at,payload) VALUES(?,?,?)
        ON CONFLICT(station_id) DO UPDATE SET received_at=excluded.received_at,payload=excluded.payload""",
        (station_id, now.isoformat(), json.dumps(payload, ensure_ascii=False)),
    )


async def handle_telemetry(station_id: str, item: TelemetryPayload) -> None:
    now = datetime.now(UTC)
    payload = item.model_dump(mode="json")
    payload["clock_skew_seconds"] = round((now - item.timestamp).total_seconds(), 3)
    runtime.hub.telemetry[station_id] = payload
    await runtime.db.execute(
        """INSERT INTO telemetry_latest(station_id,received_at,payload) VALUES(?,?,?)
        ON CONFLICT(station_id) DO UPDATE SET received_at=excluded.received_at,payload=excluded.payload""",
        (station_id, now.isoformat(), json.dumps(payload, ensure_ascii=False)),
    )
    await evaluate_telemetry(station_id, item, payload["clock_skew_seconds"])
    await runtime.hub.broadcast({"type": "telemetry", "station_id": station_id, "payload": payload})


async def evaluate_telemetry(station_id: str, item: TelemetryPayload, skew: float) -> None:
    now = datetime.now(UTC)
    if abs(skew) > 2:
        await runtime.alerts.raise_alarm(station_id, "clock_skew", Severity.WARNING, f"工站时钟偏差 {skew:.1f} 秒")
    else:
        await runtime.alerts.recover(station_id, "clock_skew")
    if item.cpu_total >= runtime.config.cpu_warning_percent:
        runtime.cpu_high_since.setdefault(station_id, now)
        duration = (now - runtime.cpu_high_since[station_id]).total_seconds()
        if duration >= 60:
            await runtime.alerts.raise_alarm(station_id, "cpu_high", Severity.WARNING, f"CPU 持续高负载 {item.cpu_total:.1f}%")
    else:
        runtime.cpu_high_since.pop(station_id, None)
        await runtime.alerts.recover(station_id, "cpu_high")
    if item.cpu_total >= runtime.config.cpu_critical_percent:
        runtime.cpu_critical_since.setdefault(station_id, now)
        if (now - runtime.cpu_critical_since[station_id]).total_seconds() >= 30:
            await runtime.alerts.raise_alarm(station_id, "cpu_critical", Severity.CRITICAL, f"CPU 严重高负载 {item.cpu_total:.1f}%")
    else:
        runtime.cpu_critical_since.pop(station_id, None)
        await runtime.alerts.recover(station_id, "cpu_critical")
    if runtime.config.temperature_critical_c is not None and item.temperatures:
        hottest = max(item.temperatures.values())
        if hottest >= runtime.config.temperature_critical_c:
            await runtime.alerts.raise_alarm(station_id, "temperature", Severity.CRITICAL, f"机械臂温度过高 {hottest:.1f}°C")
        elif runtime.config.temperature_warning_c is not None and hottest >= runtime.config.temperature_warning_c:
            await runtime.alerts.raise_alarm(station_id, "temperature", Severity.WARNING, f"机械臂温度偏高 {hottest:.1f}°C")
        else:
            await runtime.alerts.recover(station_id, "temperature")


async def handle_log(station_id: str, item: LogPayload) -> None:
    source_groups = {
        "arm_app.log": ("robot", "机械臂控制"),
        "collection.log": ("collection", "数据采集服务"),
        "task-actions.log": ("collection", "数据采集任务"),
    }
    if item.source not in source_groups:
        return
    await runtime.logs.append(station_id, item)
    if item.level in {"ERROR", "FATAL"}:
        group, label = source_groups[item.source]
        await runtime.alerts.raise_alarm(
            station_id, f"log:{group}", Severity.CRITICAL, f"{label}：{item.message[:300]}"
        )
    await runtime.hub.broadcast({"type": "log", "station_id": station_id, "payload": item.model_dump(mode="json")})


async def handle_command_result(station_id: str, payload: dict) -> None:
    job_id = payload.get("job_id")
    status = "completed" if payload.get("success") else "failed"
    await runtime.db.execute(
        "UPDATE command_jobs SET status=?,finished_at=?,result=? WHERE id=? AND station_id=?",
        (status, datetime.now(UTC).isoformat(), str(payload.get("message", "")), job_id, station_id),
    )
    if not payload.get("success"):
        await runtime.alerts.raise_alarm(station_id, "command_failed", Severity.CRITICAL, str(payload.get("message", "命令失败")))
    await runtime.hub.broadcast({"type": "command_result", "station_id": station_id, "payload": payload})


async def heartbeat_monitor() -> None:
    while True:
        await asyncio.sleep(5)
        now = datetime.now(UTC)
        rows = await runtime.db.fetch_all("SELECT id,name,ip,ssh_port,ssh_authenticated,deployment_status FROM stations")
        for row in rows:
            seen = runtime.hub.last_seen.get(row["id"])
            agent_connected = row["id"] in runtime.hub.agents and seen is not None and (now - seen).total_seconds() <= runtime.config.heartbeat_timeout_seconds
            ssh_reachable = False
            if row["ssh_authenticated"]:
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(row["ip"], row["ssh_port"]), timeout=2)
                    writer.close()
                    await writer.wait_closed()
                    ssh_reachable = True
                except (OSError, asyncio.TimeoutError):
                    pass
                await runtime.db.execute("UPDATE stations SET ssh_reachable=?,last_ssh_check=? WHERE id=?", (int(ssh_reachable), now.isoformat(), row["id"]))
            if row["deployment_status"] in {"installed", "connected"} and not agent_connected and not ssh_reachable:
                await runtime.alerts.raise_alarm(row["id"], "offline", Severity.CRITICAL, f"{row['name']} 已离线")
            else:
                await runtime.alerts.recover(row["id"], "offline")


async def retention_worker() -> None:
    while True:
        await runtime.db.cleanup(runtime.config.retention_days)
        await runtime.logs.cleanup(runtime.config.retention_days)
        await asyncio.sleep(3600)


frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/monitor-assets", StaticFiles(directory=frontend_dist / "monitor-assets"), name="monitor-assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        requested = frontend_dist / path
        return FileResponse(requested if requested.is_file() else frontend_dist / "index.html")


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("station_monitor.server.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
