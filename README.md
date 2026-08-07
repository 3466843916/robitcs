# AIRBOT 工站监控平台

workstation-monitor 是面向 AIRBOT 机械臂工站的集中监控与运维平台。系统由 FastAPI 控制中心、React 管理界面、工站 Agent 和可选模拟器组成，支持实时遥测、日志告警、进程控制、SSH 运维、数据采集代理和多种网络访问方式。

> 安全提示：项目包含远程控制、SSH 和临时公网隧道能力。请仅在受控网络中使用，部署前更换所有默认密码和共享密钥。

## 主要功能

- 工站总览：在线状态、Agent/SSH 状态、机械臂与采集进程、CPU、关节位置和电机温度。
- 实时通信：浏览器和 Agent 通过 WebSocket 接收状态、日志、告警和命令结果。
- 工站控制：机械臂/采集启停与重启、任务开关、机械臂回零、状态机复位和批量操作。
- 日志告警：按日期、工站、来源和级别查询，支持下载、清理、确认、删除和邮件通知。
- 自动接入：通过 SSH 检查 Ubuntu 22.04 工站，安装 Agent、生成配置并注册 systemd 服务。
- 远程维护：SSH 终端、文件操作，以及局域网、Tailscale 和临时公网访问辅助。
- 数据采集：代理外部采集系统，并按工站关联 project_id 完成自动登录。
- 模拟演示：模拟器可创建 1～5 个工站，持续发送遥测和日志。

## 系统结构

~~~text
浏览器（React）
    │ HTTP / WebSocket
    ▼
FastAPI 控制中心 ───── SQLite + NDJSON 日志
    │
    ├── WebSocket ─── 工站 Agent ─── systemd / ROS / AIRBOT SDK / 日志
    ├── SSH / SFTP ── 工站接入、终端与文件操作
    └── HTTP 代理 ─── 外部数据采集系统
~~~

主要目录：

~~~text
station_monitor/server/   FastAPI、数据库、告警、日志、接入和网络功能
station_monitor/agent/    Agent、采集器、日志跟踪和进程控制
station_monitor/simulator.py
                          模拟工站
frontend/src/             React + TypeScript 管理界面
frontend/public/          FSM 与网络访问帮助页面
deploy/                   Agent systemd 和 mTLS Nginx 示例
data/                     运行时数据库、日志和生成配置
tests/                    后端测试
~~~

## 环境要求

- Python 3.11+
- Node.js 18+ 与 npm（仅构建或开发前端时需要）
- Linux；自动接入的目标工站要求 Ubuntu 22.04
- 真实工站需开放 SSH，且能够回连控制中心的 Agent WebSocket
- Nginx、Tailscale 和 Cloudflare Tunnel 均为可选组件

## 快速开始

### 1. 安装后端

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[test]"
~~~

### 2. 配置

~~~bash
cp .env.example .env
~~~

开发机至少确认以下配置；生产环境必须换成强随机值：

~~~dotenv
MONITOR_HOST=127.0.0.1
MONITOR_PORT=8080
MONITOR_ADMIN_USERNAME=admin
MONITOR_ADMIN_PASSWORD=<至少 12 位强密码>
MONITOR_AGENT_SHARED_SECRET=<长随机共享密钥>
MONITOR_ALLOW_INSECURE_AGENTS=false
MONITOR_PUBLIC_AGENT_URL=wss://<控制中心地址>:8443/ws/agent
~~~

配置从项目根目录 .env 加载，变量统一使用 MONITOR_ 前缀。完整示例见 [.env.example](.env.example)。

### 3. 构建前端

后端直接托管 frontend/dist，首次运行或前端代码变更后需要构建：

~~~bash
cd frontend
npm ci
npm run build
cd ..
~~~

### 4. 启动控制中心

~~~bash
.venv/bin/station-monitor-server
~~~

默认地址：

- 管理界面：<http://127.0.0.1:8080>
- 健康检查：<http://127.0.0.1:8080/api/health>
- OpenAPI：<http://127.0.0.1:8080/docs>

若 frontend/dist 不存在，API 仍可运行，但管理页面不会被托管。

### 5. 启动模拟工站（可选）

~~~bash
.venv/bin/station-monitor-simulator \
  --server http://127.0.0.1:8080 \
  --count 5 \
  --secret change-me-before-production
~~~

--secret 必须与 MONITOR_AGENT_SHARED_SECRET 一致。模拟器会自动创建工站、每秒发送遥测、周期发送日志并响应控制命令。

## 前端开发模式

~~~bash
# 终端 1
.venv/bin/station-monitor-server

# 终端 2
cd frontend
npm run dev
~~~

访问 <http://127.0.0.1:5173>。Vite 会把 /api 和 /ws 代理到 127.0.0.1:8080。

## 核心配置

| 配置 | 默认值/示例 | 说明 |
| --- | --- | --- |
| MONITOR_HOST / MONITOR_PORT | 127.0.0.1 / 8080 | 控制中心监听地址与端口 |
| MONITOR_DATA_DIR | data | SQLite、日志和生成配置目录 |
| MONITOR_RETENTION_DAYS | 30 | 日志、审计和已恢复告警保留天数 |
| MONITOR_HEARTBEAT_TIMEOUT_SECONDS | 10 | Agent 心跳超时阈值 |
| MONITOR_ADMIN_USERNAME / PASSWORD | 见 .env.example | 浏览器登录凭据，部署前必须修改 |
| MONITOR_AGENT_SHARED_SECRET | 初始化示例值 | Agent 注册共享密钥，部署前必须修改 |
| MONITOR_ALLOW_INSECURE_AGENTS | 代码默认 true | 生产环境应设为 false 并启用 mTLS |
| MONITOR_PUBLIC_AGENT_URL | ws://127.0.0.1:8080/ws/agent | 写入 Agent 配置的回连地址 |
| MONITOR_ACQUISITION_BASE_URL | 现场采集服务地址 | 数据采集反向代理上游 |
| MONITOR_CPU_* / TEMPERATURE_* | 见配置类 | CPU 与温度告警阈值 |
| MONITOR_SMTP_* / ALERT_EMAIL | 空 | 可选 SMTP 告警邮件 |
| MONITOR_*_COMMAND / *_UNIT | AIRBOT 默认命令 | 工站进程、任务、回零和复位命令 |

修改 .env 后需要重启控制中心。不要提交真实 .env、证书私钥或工站密码。

## 工站 Agent

Agent 使用 JSON 配置：

~~~json
{
  "station_id": "站点 UUID",
  "station_ip": "192.168.31.13",
  "server_url": "wss://monitor.example:8443/ws/agent",
  "secret": "与服务端一致的共享密钥",
  "robot_unit": "airbot-robot.service",
  "collection_unit": "airbot-collection.service",
  "ros_domain_id": 0,
  "joint_topic": "/joint_states",
  "temperature_topics": [],
  "log_paths": ["/userdata/storage/arm_app/last/log/arm_app.log"]
}
~~~

手动运行：

~~~bash
.venv/bin/station-monitor-agent --config /etc/station-monitor/agent.json
~~~

[Agent systemd 示例](deploy/station-monitor-agent.service) 展示了生产工站运行方式。管理界面的接入流程通过一次 SSH 密码登录完成检查与安装；密码用于当次连接，不写入数据库。已知的五个现场 IP 会匹配各自任务命令，其余 IP 使用 .env 中的通用命令。

### Agent mTLS

[Agent mTLS Nginx 示例](deploy/nginx-agent-mtls.conf) 在 8443 上反向代理 WebSocket。生产环境建议：

1. 为控制中心和 Agent 签发证书。
2. 由 Nginx 验证客户端证书并设置 X-SSL-Client-Verify。
3. 设置 MONITOR_ALLOW_INSECURE_AGENTS=false。
4. 将 MONITOR_PUBLIC_AGENT_URL 改为 wss://...:8443/ws/agent。

## 数据、接口与端口

默认数据位置：

- data/monitor.db：工站、最新遥测、告警、命令、日志索引和审计。
- data/logs/<station>/<source>/<date>.ndjson：日志原文。
- data/known_hosts：SSH 主机密钥记录。
- data/workstation-monitor-nginx.conf：页面生成的局域网 Nginx 配置。

| 端口 | 用途 |
| --- | --- |
| 8080 | 页面、REST API、浏览器/Agent WebSocket |
| 5173 | Vite 开发服务器 |
| 8088 | 可选 Nginx 局域网管理入口 |
| 8081 | 可选 Nginx 数据采集代理 |
| 8443 | 示例 mTLS Agent WebSocket |
| 22 | 工站 SSH/SFTP |
| 9090 | 工站 FSM 服务（现场配置） |

主要接口：

- REST：/api/auth/*、/api/stations、/api/commands、/api/alarms、/api/logs、/api/network/*
- 浏览器实时通道：/ws/browser
- Agent 通道：/ws/agent
- SSH 终端：/ws/terminal/{station_id}

除登录、健康检查和 Agent 通道等必要入口外，浏览器 API 受登录 Cookie 保护。以运行后的 /docs 为准。

## 网络访问

- 局域网：页面可生成 Nginx 配置，8088 访问控制中心，8081 访问采集代理。自动启用依赖无交互 sudo，失败时按页面提示手动配置。
- 远程私网：安装并登录 Tailscale 后，页面返回 Tailscale IPv4，访问端口仍为 8088。
- 临时公网：优先使用 Cloudflare Quick Tunnel，失败时尝试 localhost.run。临时地址不适合作为正式生产入口。

开启临时公网访问前，后端要求 MONITOR_ADMIN_PASSWORD 至少 12 位。正式部署应使用 HTTPS、mTLS、访问控制和防火墙白名单。

## 测试与质量检查

~~~bash
.venv/bin/python -m compileall -q station_monitor
.venv/bin/pytest -q

cd frontend
npm run typecheck
npm run build
~~~

## 常见问题

### 页面打不开或 Nginx 返回 502

~~~bash
ss -ltnp | grep ':8080'
curl -sS http://127.0.0.1:8080/api/health
nginx -t
~~~

先确认后端监听 8080，再检查 8088 的 Nginx upstream。

### Agent 一直离线

确认 server_url 可从工站访问、共享密钥一致、系统时间正确：

~~~bash
sudo systemctl status station-monitor-agent.service
sudo journalctl -u station-monitor-agent.service -n 200 --no-pager
~~~

### 工站接入或 SSH 失败

控制中心必须能访问目标 IP:22。请使用目标工站的 Linux 账号，而不是管理页面账号。首次接入要求 Ubuntu 22.04，并需要足够权限安装依赖和 systemd 服务。

### 数据采集页空白

检查控制中心到 MONITOR_ACQUISITION_BASE_URL 的网络和凭据，以及工站的 acquisition_project_id。跨网络后失效通常是采集上游路由不可达。

### 日志或数据库持续增长

确认 MONITOR_RETENTION_DAYS 合理、服务持续运行且 data/ 可写。清理任务每小时执行一次；清理前先备份 data/。

## 备份与升级

至少备份 data/ 和部署机 .env；.env 含敏感信息，应加密保存。升级前停止服务、备份数据、更新依赖、重新构建前端并运行测试。

更完整的部署、验收和排障步骤见 [运行文档.html](运行文档.html)。
