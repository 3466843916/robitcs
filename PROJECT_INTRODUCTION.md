# AIRBOT 五工站监控平台

## 项目简介

`workstation-monitor` 是面向 AIRBOT 机械臂工站的监控、日志管理与远程控制平台。系统由 Python/FastAPI 后端、部署在各工站上的 Agent，以及 React + Vite 前端组成，可集中查看多台工站的在线状态、进程状态、资源占用、机械臂关节数据和电机温度，并通过 WebSocket 实时接收遥测与日志。

平台同时提供告警管理、操作审计、日志查询与下载、工站自动接入、SSH 终端和常用控制命令等能力，适合在内网环境中对多工站进行统一运维。

## 主要功能

- **工站总览**：展示工站在线状态、Agent 连接状态、机器人与数据采集进程状态、CPU 使用率、关节位置和温度。
- **实时通信**：浏览器、服务端和工站 Agent 通过 WebSocket 交换遥测、日志、告警和控制结果。
- **进程控制**：按白名单启动、停止、重启机器人和采集服务，支持回零、状态复位、任务开关和批量命令。
- **日志中心**：接收 Agent 日志，按工站、日期、来源和级别查询，支持错误日志筛选、文件下载、删除和保留期清理。
- **告警管理**：根据心跳、CPU、温度、进程和日志状态产生告警，支持确认、批量确认和删除；可选 SMTP 邮件通知。
- **工站接入**：通过 SSH 检查并安装工站 Agent、systemd 服务和必要配置，记录部署状态。
- **远程运维**：提供基于 WebSocket 的 SSH 终端及文件浏览、上传和下载能力，并提供网络访问辅助接口。
- **模拟环境**：内置模拟器，可在没有真实工站时生成遥测、日志和命令执行结果，用于联调和演示。

## 系统架构

```text
React + Vite 前端  ◄──HTTP / WebSocket──►  FastAPI 监控服务  ◄──WebSocket──►  工站 Agent
仪表盘 / 日志 / 终端                         SQLite / 告警 / API                  遥测 / 日志 / 控制
                                                                              ▲
                                                                              │
                                                                         模拟工站
```

## 目录结构

```text
station_monitor/
├── server/       # FastAPI API、WebSocket、数据库、告警、日志和工站接入
├── agent/        # 工站端 Agent、遥测采集、日志流和进程控制
└── simulator.py  # 模拟多工站数据和命令执行
frontend/src/     # React 管理界面
deploy/           # nginx、systemd 和 mTLS 部署示例
data/             # 运行时 SQLite 数据库和日志（默认生成）
tests/            # Python 单元测试
```

## 快速开始

### 1. 准备 Python 环境

项目要求 Python 3.11 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

### 2. 配置服务

复制并编辑环境变量文件：

```bash
cp .env.example .env
```

至少应在生产环境中修改 `MONITOR_ADMIN_PASSWORD`、`MONITOR_AGENT_SHARED_SECRET`，并关闭 `MONITOR_ALLOW_INSECURE_AGENTS`。

### 3. 启动后端

```bash
station-monitor-server
```

默认监听 `127.0.0.1:8080`，也可以直接使用 Uvicorn：

```bash
uvicorn station_monitor.server.main:app --host 127.0.0.1 --port 8080
```

### 4. 启动前端开发服务器

```bash
cd frontend
npm ci
npm run dev
```

生产构建使用 `npm run build`。也可以使用 `deploy/` 下的 nginx 配置进行反向代理和 TLS/mTLS 部署。

### 5. 使用模拟器联调

后端启动后执行：

```bash
station-monitor-simulator --server http://127.0.0.1:8080 --count 5
```

模拟器会自动创建最多五个工站，持续发送遥测和日志，并响应常用控制命令。浏览器打开 `http://127.0.0.1:8080` 即可查看平台。

## 工站 Agent

真实工站运行 `station-monitor-agent`，通过 JSON 配置文件指定工站 ID、服务端 WebSocket 地址、共享密钥、ROS/SDK 采集命令和日志路径。Agent 负责心跳、CPU 和机械臂遥测采集、SDK 数据解析、日志跟踪，以及执行服务控制、回零和任务命令。\
`deploy/station-monitor-agent.service` 提供 systemd 服务示例。

## API 与实时通道

- REST API：`/api/health`、`/api/stations`、`/api/commands`、`/api/alarms`、`/api/logs` 等；
- 浏览器实时通道：`/ws/browser`；
- Agent 通道：`/ws/agent`；
- SSH 终端通道：`/ws/terminal/{station_id}`。

启动服务后可访问 `/docs` 查看 FastAPI 自动生成的 OpenAPI 文档。

## 测试与质量检查

```bash
pytest
cd frontend
npm run typecheck
npm run build
```

## 安全提示

平台包含远程命令执行和 SSH 终端能力，应仅部署在受控网络中。生产环境请使用强随机共享密钥和管理员密码，启用 HTTPS/mTLS，限制管理端口访问范围，并审查 `.env` 中的命令、SSH 凭据和邮件配置。

## 技术栈

- 后端：Python 3.11、FastAPI、Uvicorn、Pydantic Settings、SQLite/aiosqlite、AsyncSSH、WebSocket；
- 前端：React、TypeScript、Vite、Lucide、xterm.js；
- 部署：systemd、nginx，可选客户端证书认证（mTLS）。

