import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, AlertTriangle, Bell, Bot, CheckCircle2, ChevronRight, CircleHelp, CircleStop, Cpu, Download, FileText, Folder, Gauge, Globe2, Home, Loader2, LogOut, Pencil, Play, Plus, RefreshCw, RotateCcw, Server, SquareTerminal, Thermometer, Trash2, Upload, Wifi, WifiOff, X } from "lucide-react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

type ProcessState = "unknown" | "inactive" | "starting" | "running" | "stopping" | "failed";
type Station = {
  id: string; name: string; ip: string; online: boolean; agent_online?: boolean; deployment_status: string; deployment_message?: string; ssh_username?: string; ssh_port?: number; ssh_reachable?: boolean; ros_domain_id?: number; joint_topic?: string; temperature_topics?: string[]; notes?: string; acquisition_project_id?: number;
  last_heartbeat?: string; clock_skew_seconds?: number; robot_state: ProcessState; collection_state: ProcessState;
  cpu_total?: number; cpu_agent?: number; cpu_robot?: number; cpu_collection?: number; cpu_per_core?: number[];
  joints: Record<string, number>; temperatures: Record<string, number>; active_alarm_count: number;
};
type LogEntry = {station_id: string; timestamp: string; source: string; level: string; sequence: number; message: string};
type Alarm = {id: string; station_id: string; severity: string; message: string; status: string; first_at: string; last_at: string};
type LogFile = {station_id: string; source: string; log_date: string; path: string; bytes: number};
type LogPage = {items:LogEntry[];total:number;page:number;page_size:number;pages:number};

const stateLabel: Record<ProcessState, string> = {unknown: "未知", inactive: "未运行", starting: "启动中", running: "运行中", stopping: "停止中", failed: "通信异常"};
const stationColors = ["#4c7dff", "#8b5cf6", "#0ea5a8", "#f59e0b", "#ec4899"];

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {headers: {"Content-Type": "application/json", ...(options?.headers || {})}, ...options});
  if (!response.ok) { const body = await response.json().catch(() => ({detail: response.statusText})); throw new Error(body.detail || "请求失败"); }
  return response.status === 204 ? undefined as T : response.json();
}

function formatTime(value?: string) { return value ? new Intl.DateTimeFormat("zh-CN", {hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date(value)) : "—"; }
function localDate(value = new Date()) { const offset = value.getTimezoneOffset() * 60000; return new Date(value.getTime() - offset).toISOString().slice(0, 10); }
function isToday(value: string) { return localDate(new Date(value)) === localDate(); }
function pct(value?: number) { return value == null ? "—" : `${value.toFixed(1)}%`; }
function bytes(value: number) { if (value < 1024) return `${value} B`; if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1048576).toFixed(1)} MB`; }
function sourceLabel(source: string) { return ({"arm_app.log":"机械臂控制日志","collection.log":"数据采集服务日志","task-actions.log":"数据采集任务日志"} as Record<string,string>)[source] || source; }
function matchesLogSource(source: string, group: "all"|"robot"|"collection") { return group === "all" || (group === "robot" ? source === "arm_app.log" : ["collection.log","task-actions.log"].includes(source)); }
function acquisitionUrl(stationId:string) { return `${location.origin}/_airbot_login?station_id=${encodeURIComponent(stationId)}`; }
function fsmUrl(station:Station) { return "/fsm_debug_ui.html?target=" + encodeURIComponent("http://" + station.ip + ":9090"); }
function temperatureLabel(name: string) { if (name.startsWith("eef_motor_")) return "夹爪电机 " + name.slice(10); if (name.startsWith("motor_")) return "关节电机 " + name.slice(6); return name; }

export default function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [stations, setStations] = useState<Station[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<"welcome" | "overview" | "logs" | "alarms" | "fsm">("welcome");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [logStation, setLogStation] = useState<string | null>(null);
  const [logRefresh, setLogRefresh] = useState(0);
  const [logSource, setLogSource] = useState<"all"|"robot"|"collection">("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [logPage, setLogPage] = useState(1);
  const [logPages, setLogPages] = useState(1);
  const [logTotal, setLogTotal] = useState(0);
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [files, setFiles] = useState<LogFile[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [terminalStation, setTerminalStation] = useState<Station | null>(null);
  const [editStation, setEditStation] = useState<Station | null>(null);
  const [stationPage, setStationPage] = useState(1);
  const [busy, setBusy] = useState<string | null>(null);
  const [networkInfo, setNetworkInfo] = useState<{url?:string;message:string;ok:boolean;scope:"lan"|"remote"|"internet"}|null>(null);
  const [commandError, setCommandError] = useState<{stationId:string;target:string;action:string;message:string}|null>(null);
  const [toast, setToast] = useState<{kind: "ok" | "error"; text: string} | null>(null);
  const [connected, setConnected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const retry = useRef<number | undefined>(undefined);
  const logViewRef = useRef({tab, logStation, logSource, errorsOnly, logPage});

  useEffect(() => { logViewRef.current = {tab, logStation, logSource, errorsOnly, logPage}; }, [tab, logStation, logSource, errorsOnly, logPage]);

  const load = useCallback(async () => {
    const [stationData, alarmData, fileData] = await Promise.all([
      api<Station[]>("/api/stations"), api<Alarm[]>("/api/alarms"), api<LogFile[]>("/api/logs/files")
    ]);
    setStations(stationData); setAlarms(alarmData); setFiles(fileData);
  }, []);

  useEffect(() => { api<{authenticated:boolean}>("/api/auth/me").then(()=>setAuthenticated(true)).catch(()=>setAuthenticated(false)).finally(()=>setAuthChecked(true)); }, []);
  useEffect(() => { if(authenticated) load().catch(e => setToast({kind: "error", text: e.message})); }, [load,authenticated]);
  useEffect(() => {
    if(!authenticated) return;
    let socket: WebSocket;
    let disposed = false;
    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/browser`);
      socket.onopen = () => setConnected(true);
      socket.onclose = () => { setConnected(false); if(!disposed) retry.current = window.setTimeout(connect, 2000); };
      socket.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === "snapshot") setStations(message.stations);
        else if (message.type === "telemetry") setStations(current => current.map(s => s.id === message.station_id ? {...s, online: true, last_heartbeat: new Date().toISOString(), ...message.payload} : s));
        else if (message.type === "log") {
          const entry: LogEntry = {station_id: message.station_id, ...message.payload};
          const view = logViewRef.current;
          const matches = isToday(entry.timestamp) && matchesLogSource(entry.source, view.logSource) && (!view.logStation || view.logStation === entry.station_id) && (!view.errorsOnly || ["ERROR", "FATAL"].includes(entry.level));
          if (view.tab === "logs" && view.logPage === 1 && matches) {
            setLogs(current => [entry, ...current].slice(0, 50));
            setLogTotal(current => {
              const next = current + 1;
              setLogPages(Math.max(1, Math.ceil(next / 50)));
              return next;
            });
          }
        }
        else load().catch(() => undefined);
      };
    };
    connect();
    return () => { disposed=true; window.clearTimeout(retry.current); socket?.close(); };
  }, [load,authenticated]);

  useEffect(() => {
    if(!authenticated||tab!=="logs") return;
    const params=new URLSearchParams({page:String(logPage),page_size:"50",log_date:localDate()});
    if(logStation) params.set("station_id",logStation);
    if(logSource !== "all") params.set("source_group",logSource);
    if(errorsOnly) params.set("level","ERROR");
    api<LogPage>(`/api/logs?${params}`).then(data=>{setLogs(data.items);setLogPages(data.pages);setLogTotal(data.total);}).catch(error=>setToast({kind:"error",text:error.message}));
  },[authenticated,tab,logStation,logSource,errorsOnly,logPage,logRefresh]);
  useEffect(() => { if (toast) { const timer = setTimeout(() => setToast(null), 3500); return () => clearTimeout(timer); } }, [toast]);

  const activeAlarms = alarms.filter(a => a.status === "active");
  const current = stations.find(s => s.id === selected);
  const orderedStations = useMemo(() => [...stations].sort((a,b) => Number(Boolean(b.agent_online||b.online))-Number(Boolean(a.agent_online||a.online)) || a.name.localeCompare(b.name, "zh-CN")), [stations]);
  const stationPages = Math.max(1, Math.ceil(orderedStations.length / 4));
  const pagedStations = orderedStations.slice((stationPage - 1) * 4, stationPage * 4);
  const visibleLogs = useMemo(() => logs.filter(l => matchesLogSource(l.source, logSource) && (!logStation || l.station_id === logStation) && (!errorsOnly || ["ERROR", "FATAL"].includes(l.level))).sort((a, b) => b.timestamp.localeCompare(a.timestamp) || b.station_id.localeCompare(a.station_id) || b.sequence - a.sequence), [logs, logStation, logSource, errorsOnly]);

  async function command(stationId: string, target: string, action: string) {
    const dangerous = action === "restart" || target === "all" || target === "collection_service";
    const commandName = target === "all" ? "全部停止" : target === "collection_service" ? "停止数据采集程序" : target + " " + action;
    if (dangerous && !confirm("确认对该工站执行“" + commandName + "”吗？")) return;
    setBusy(`${stationId}:${target}:${action}`);
    try { await api(`/api/stations/${stationId}/commands`, {method: "POST", body: JSON.stringify({target, action})}); setToast({kind: "ok", text: "命令已下发"}); }
    catch (e) { const message=(e as Error).message; setCommandError({stationId,target,action,message}); setToast({kind: "error", text: message}); }
    finally { setBusy(null); }
  }

  async function deleteLogFiles(paths: string[]) {
    if (!confirm(`确认删除选中的 ${paths.length} 个日志文件吗？对应实时日志记录也会删除。`)) return;
    try {
      const result = await api<{deleted:number}>("/api/logs/files", {method:"DELETE", body:JSON.stringify({paths})});
      await load();
      setLogRefresh(value => value + 1);
      setToast({kind:"ok", text:`已删除 ${result.deleted} 个日志文件`});
    } catch (error) {
      setToast({kind:"error", text:(error as Error).message});
      throw error;
    }
  }

  async function clearTodayLogs() {
    const scope = logStation ? stations.find(station => station.id === logStation)?.name || "当前工站" : "全部工站";
    if (!confirm(`确认清空${scope}今日的全部日志吗？此操作不可撤销。`)) return;
    setBusy("logs:clear");
    try {
      const params = new URLSearchParams({log_date: localDate()});
      if (logStation) params.set("station_id", logStation);
      if (logSource !== "all") params.set("source_group", logSource);
      const result = await api<{deleted:number}>(`/api/logs/today?${params}`, {method:"DELETE"});
      setLogs([]); setLogPage(1); setLogTotal(0); setLogPages(1);
      await load();
      setLogRefresh(value => value + 1);
      setToast({kind:"ok", text:`已清空 ${result.deleted} 条今日日志`});
    } catch (error) { setToast({kind:"error", text:(error as Error).message}); }
    finally { setBusy(null); }
  }

  async function enableNetworkAccess() {
    try {
      const result=await api<{ip:string;url:string}>("/api/network/nginx",{method:"POST"});
      setNetworkInfo({ok:true,scope:"lan",url:result.url,message:`局域网 IP：${result.ip}\n访问地址：${result.url}`});
    } catch(error) { setNetworkInfo({ok:false,scope:"lan",message:(error as Error).message}); }
  }

  async function enableRemoteAccess() {
    try {
      const result=await api<{ip:string;url:string}>("/api/network/remote",{method:"POST"});
      setNetworkInfo({ok:true,scope:"remote",url:result.url,message:`远程网络 IP：${result.ip}\n访问地址：${result.url}\n访问设备需加入同一个 Tailscale 网络。`});
    } catch(error) {
      setNetworkInfo({ok:false,scope:"remote",message:(error as Error).message});
    }
  }

  async function enableInternetAccess() {
    try {
      const result=await api<{url:string}>("/api/network/internet",{method:"POST"});
      setNetworkInfo({ok:true,scope:"internet",url:result.url,message:`互联网访问已开启\n公网地址：${result.url}\n任何获得该地址的人都可以打开登录页。`});
    } catch(error) {
      setNetworkInfo({ok:false,scope:"internet",message:(error as Error).message});
    }
  }

  async function logout() { try { await api<void>("/api/auth/logout",{method:"POST"}); } finally { setAuthenticated(false); } }

  async function refreshAll() {
    setRefreshing(true);
    try {
      await load();
      if (tab === "logs") setLogRefresh(value => value + 1);
      setToast({kind:"ok", text:"数据已刷新"});
    } catch (error) {
      setToast({kind:"error", text:(error as Error).message});
    } finally {
      setRefreshing(false);
    }
  }

  async function acknowledgeAllAlarms() {
    const count = alarms.filter(alarm => alarm.status === "active").length;
    if (!count) return;
    try {
      await api<void>("/api/alarms/acknowledge-all", {method:"POST"});
      await load();
      setToast({kind:"ok", text:"已将 " + count + " 条告警设为已读"});
    } catch (error) { setToast({kind:"error", text:(error as Error).message}); }
  }

  async function deleteAlarms(ids: string[]) {
    if (!confirm("确认删除选中的 " + ids.length + " 条告警吗？")) return false;
    try {
      const result = await api<{deleted:number}>("/api/alarms/delete-batch", {method:"POST", body:JSON.stringify({ids})});
      await load();
      setToast({kind:"ok", text:"已删除 " + result.deleted + " 条告警"});
      return true;
    } catch (error) {
      setToast({kind:"error", text:(error as Error).message});
      return false;
    }
  }

  if (!authChecked) return <div className="login-loading"><Loader2 className="spin"/>正在验证登录状态…</div>;
  if (!authenticated) return <LoginPage onLogin={async(username,password)=>{await api<void>("/api/auth/login",{method:"POST",body:JSON.stringify({username,password})});setAuthenticated(true)}}/>;

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Activity size={22}/></div><div><strong>AIRBOT</strong><span>工站控制中心</span></div></div>
      <nav>
        <button className={tab === "welcome" ? "active" : ""} onClick={() => setTab("welcome")}><Home/>欢迎首页</button>
        <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}><Gauge/>运行总览</button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}><FileText/>实时日志</button>
        <button className={tab === "alarms" ? "active" : ""} onClick={() => setTab("alarms")}><Bell/>告警中心{activeAlarms.length > 0 && <b>{activeAlarms.length}</b>}</button><button className={tab === "fsm" ? "active" : ""} onClick={() => setTab("fsm")}><Gauge/>状态机监控</button>
      </nav>
      <div className="side-status"><span className={connected ? "dot live" : "dot"}/><div><strong>{connected ? "中心服务正常" : "正在重新连接"}</strong><small>{connected ? "实时通道已连接" : "数据可能暂时延迟"}</small></div></div><button className="logout" onClick={logout}><LogOut/>退出登录</button>
    </aside>

    <main className={tab === "welcome" ? "welcome-main" : undefined}>
      <header><div><h1>{tab === "welcome" ? "欢迎使用" : tab === "overview" ? "运行总览" : tab === "logs" ? "实时日志" : tab === "alarms" ? "告警中心" : "状态机监控"}</h1><p>{new Date().toLocaleDateString("zh-CN", {year: "numeric", month: "long", day: "numeric", weekday: "long"})}</p></div><div className="header-actions"><div className="access-action"><button className="ghost" onClick={enableNetworkAccess}><Globe2/>开启局域网访问</button><a className="network-help" href="/nginx-help.html" target="_blank" rel="noreferrer"><CircleHelp/>局域网开启说明</a></div><div className="access-action"><button className="ghost remote" onClick={enableRemoteAccess}><Wifi/>开启远程访问</button><a className="network-help" href="/remote-access-help.html" target="_blank" rel="noreferrer"><CircleHelp/>远程访问开启说明</a></div><div className="access-action"><button className="ghost internet" onClick={enableInternetAccess}><Globe2/>开启互联网访问</button><a className="network-help" href="/internet-access-help.html" target="_blank" rel="noreferrer"><CircleHelp/>互联网访问开启说明</a></div><button className="ghost" disabled={refreshing} onClick={refreshAll}>{refreshing?<Loader2 className="spin"/>:<RefreshCw size={16}/>} {refreshing?"刷新中":"刷新"}</button><button className="primary" onClick={() => setAddOpen(true)}><Plus size={17}/>添加工站</button></div></header>

      {tab === "welcome" && <WelcomeDashboard stations={orderedStations} alarms={alarms} onNavigate={setTab}/>}
      {tab === "overview" && <>
        <section className="metrics">
          <Metric icon={<Server/>} label="工站总数" value={`${stations.length}`} suffix="台" color="blue"/>
          <Metric icon={<Wifi/>} label="在线工站" value={`${stations.filter(s => s.online).length}`} suffix="台" color="green"/>
          <Metric icon={<Bot/>} label="运行中机械臂" value={`${stations.filter(s => s.robot_state === "running").length}`} suffix="台" color="purple"/>
          <Metric icon={<AlertTriangle/>} label="活动告警" value={`${activeAlarms.length}`} suffix="条" color={activeAlarms.length ? "red" : "green"}/>
        </section>
        <div className="section-title"><div><h2>工站状态</h2><span>点击工站查看关节与温度详情</span></div><div className="legend"><i className="online"/>在线<i/>离线</div></div>
        <section className="station-grid">
          {pagedStations.map(station => <StationCard key={station.id} station={station} color={stationColors[stations.indexOf(station)]} onSelect={() => setSelected(station.id)} onTerminal={() => setTerminalStation(station)} onCommand={command} busy={busy}/>) }
          {stations.length === 0 && <div className="empty-card"><Server/><h3>还没有工站</h3><p>添加第一台工站，或运行模拟器体验完整界面。</p><button className="primary" onClick={() => setAddOpen(true)}><Plus size={16}/>添加工站</button></div>}
        </section>
        <div className="station-pagination"><button disabled={stationPage<=1} onClick={()=>setStationPage(page=>page-1)}>上一页</button><span>第 {stationPage} / {stationPages} 页 · 每页 4 台</span><button disabled={stationPage>=stationPages} onClick={()=>setStationPage(page=>page+1)}>下一页</button></div>
      </>}

      {tab === "logs" && <LogView stations={orderedStations} selected={logStation} setSelected={value=>{setLogStation(value);setLogPage(1)}} logs={visibleLogs} sourceGroup={logSource} setSourceGroup={value=>{setLogSource(value);setLogPage(1)}} errorsOnly={errorsOnly} setErrorsOnly={value=>{setErrorsOnly(value);setLogPage(1)}} files={files} page={logPage} pages={logPages} total={logTotal} onPage={setLogPage} onDelete={deleteLogFiles} onClear={clearTodayLogs} clearing={busy==="logs:clear"}/>} 
      {tab === "fsm" && <FsmMonitor stations={orderedStations}/>}
      {tab === "alarms" && <AlarmView alarms={alarms} stations={orderedStations} onAck={async id => {await api("/api/alarms/"+id+"/acknowledge", {method:"POST"}); await load();}} onAckAll={acknowledgeAllAlarms} onDelete={deleteAlarms}/>}
    </main>

    {current && <DetailDrawer station={current} color={stationColors[stations.findIndex(s => s.id === current.id)]} onClose={() => setSelected(null)} onEdit={() => setEditStation(current)} onTerminal={() => setTerminalStation(current)} onCommand={command} busy={busy}/>} 
    {addOpen && <AddStation onClose={() => setAddOpen(false)} onAdded={async () => {setAddOpen(false); await load(); setToast({kind: "ok", text: "工站已加入安装队列"});}}/>}
    {terminalStation && <TerminalWindow station={terminalStation} onClose={() => setTerminalStation(null)}/>} 
    {editStation && <EditStation station={editStation} onClose={()=>setEditStation(null)} onSaved={async()=>{setEditStation(null);await load();setToast({kind:"ok",text:"工站信息已更新"})}} onDeleted={async()=>{setEditStation(null);setSelected(null);await load();setToast({kind:"ok",text:"工站已删除"})}}/>}
    {networkInfo && <NetworkModal info={networkInfo} onClose={()=>setNetworkInfo(null)}/>}
    {commandError && <CommandErrorModal error={commandError} station={stations.find(item=>item.id===commandError.stationId)} onClose={()=>setCommandError(null)} onRetry={()=>{const error=commandError;setCommandError(null);command(error.stationId,error.target,error.action)}}/>}
    {toast && <div className={`toast ${toast.kind}`}>{toast.kind === "ok" ? <CheckCircle2/> : <AlertTriangle/>}{toast.text}</div>}
  </div>;
}

function LoginPage({onLogin}:{onLogin:(username:string,password:string)=>Promise<void>}) {
  const [username,setUsername]=useState("admin"); const [password,setPassword]=useState(""); const [error,setError]=useState("");
  const [submitting,setSubmitting]=useState(false);
  async function submit(event:React.FormEvent){event.preventDefault();setSubmitting(true);setError("");try{await onLogin(username,password)}catch(exc){setError((exc as Error).message)}finally{setSubmitting(false)}}
  return <div className="login-page"><section className="login-visual"><div className="brand-mark"><Activity/></div><h1>AIRBOT 工站控制中心</h1><p>集中监控机械臂、数据采集、日志与告警</p><div className="login-orbits"><span/><span/><Bot/></div></section><form className="login-card" onSubmit={submit}><small>WELCOME BACK</small><h2>登录控制中心</h2><p>请输入管理员账号继续</p><label>账号<input autoFocus value={username} onChange={e=>setUsername(e.target.value)}/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)}/></label>{error&&<div className="form-error">{error}</div>}<button className="primary" disabled={submitting}>{submitting?<Loader2 className="spin"/>:"登录"}</button><em>默认账号 admin · 密码 040712</em></form></div>;
}

function WelcomeDashboard({stations,alarms,onNavigate}:{stations:Station[];alarms:Alarm[];onNavigate:(tab:"welcome"|"overview"|"logs"|"alarms")=>void}) {
  const online=stations.filter(s=>s.agent_online||s.online).length, offline=stations.length-online, robot=stations.filter(s=>s.robot_state==="running").length, collection=stations.filter(s=>s.collection_state==="running").length, active=alarms.filter(a=>a.status==="active").length;
  const percent=(value:number)=>stations.length?Math.round(value/stations.length*100):0;
  const health=active?"存在需要处理的告警":offline?`${offline} 台工站当前离线`:stations.length?"所有工站连接正常":"等待接入首台工站";
  return <section className="welcome-dashboard">
    <div className="welcome-hero">
      <div className="welcome-copy">
        <div className={`hero-status ${active?"warning":offline?"partial":"healthy"}`}><i/>{health}</div>
        <span className="hero-eyebrow">AIRBOT OPERATIONS</span>
        <h2>欢迎回来，管理员</h2>
        <p>工站状态、控制链路与异常信息已汇总，关键运行情况一目了然。</p>
        <div className="hero-metrics">
          <div><strong>{stations.length}</strong><span>工站总数</span></div>
          <div><strong>{online}</strong><span>当前在线</span></div>
          <div className={active?"attention":""}><strong>{active}</strong><span>活动告警</span></div>
        </div>
        <div className="hero-actions"><button className="primary" onClick={()=>onNavigate("overview")}><Gauge/>查看运行总览</button><button onClick={()=>onNavigate("logs")}><FileText/>查看实时日志</button></div>
      </div>
      <div className="hero-radar" aria-hidden="true"><div className="radar-core"><Activity/><span>实时监控</span></div><i/><i/><i/></div>
    </div>
    <div className="welcome-charts">
      <article className="online-card"><header><div><strong>工站在线率</strong><small>设备连接概况</small></div><span className="card-icon blue"><Wifi/></span></header><div className="donut" style={{"--value":percent(online)} as React.CSSProperties}><b>{percent(online)}%</b><small>{online} / {stations.length} 在线</small></div><div className="status-legend"><span><i className="online"/>在线 {online}</span><span><i/>离线 {offline}</span></div></article>
      <article className="running-card"><header><div><strong>运行状态</strong><small>核心服务实时负载</small></div><span className="card-icon purple"><Bot/></span></header><ChartBar label="机械臂运行" value={robot} total={stations.length} color="#7357e8"/><ChartBar label="数据采集运行" value={collection} total={stations.length} color="#16a36a"/><ChartBar label="监控在线" value={online} total={stations.length} color="#4c72e8"/></article>
      <article className={`alarm-card ${active?"has-alarm":""}`}><header><div><strong>活动告警</strong><small>当前未处理异常</small></div><span className="card-icon red"><AlertTriangle/></span></header><div className={`alarm-summary ${active?"hot":""}`}><b>{active}</b><span>{active?"条告警等待处理":"当前运行平稳"}</span><small>{active?"建议尽快进入告警中心查看":"暂无需要处理的异常"}</small></div><button onClick={()=>onNavigate("alarms")}>进入告警中心<ChevronRight/></button></article>
    </div>
    <section className="welcome-stations">
      <header><div><h3>工站概览</h3><p>快速查看全部工站的连接与程序状态</p></div><button onClick={()=>onNavigate("overview")}>查看完整总览<ChevronRight/></button></header>
      <div className="welcome-station-grid">
        {stations.slice(0,10).map(station=>{const connected=Boolean(station.agent_online||station.online);return <article key={station.id} className={connected?"connected":"offline"}><header><span className="welcome-station-mark"><Bot/></span><div><strong>{station.name}</strong><small>{station.ip}</small></div><span className={`station-signal ${connected?"on":""}`}><i/>{connected?"在线":"离线"}</span></header><div className="welcome-processes"><span className={station.robot_state}><i/>机械臂 {stateLabel[station.robot_state]}</span><span className={station.collection_state}><i/>数采 {stateLabel[station.collection_state]}</span></div><footer><span>CPU <b>{pct(station.cpu_total)}</b></span>{station.active_alarm_count>0?<span className="station-alert"><AlertTriangle/>{station.active_alarm_count} 条告警</span>:<span className="station-ok"><CheckCircle2/>状态平稳</span>}</footer></article>})}
        {stations.length===0&&<div className="welcome-stations-empty"><Server/><span>暂无工站，添加工站后将在这里显示状态</span></div>}
      </div>
    </section>
  </section>;
}
function ChartBar({label,value,total,color}:{label:string;value:number;total:number;color:string}) { const width=total?value/total*100:0;return <div className="chart-bar"><label><span>{label}</span><b>{value} / {total}</b></label><i><em style={{width:width+"%",background:color}}/></i></div>; }
function NetworkModal({info,onClose}:{info:{url?:string;message:string;ok:boolean;scope:"lan"|"remote"|"internet"};onClose:()=>void}) { const label=info.scope==="internet"?"互联网访问":info.scope==="remote"?"远程访问":"局域网访问";const help=info.scope==="internet"?"/internet-access-help.html":info.scope==="remote"?"/remote-access-help.html":"/nginx-help.html";return <div className="scrim centered"><section className="command-error-modal network-modal"><div className={`error-symbol ${info.ok?"success":""}`}>{info.ok?<Wifi/>:<AlertTriangle/>}</div><h2>{info.ok?`${label}已开启`:`未能开启${label}`}</h2><pre>{info.message}</pre>{info.url&&<a href={info.url} target="_blank" rel="noreferrer">点击打开 {info.url}</a>}<div>{!info.ok&&<a className="help-button" href={help} target="_blank" rel="noreferrer">查看{label}开启说明</a>}<button className="primary" onClick={onClose}>知道了</button></div></section></div>; }
function CommandErrorModal({error,station,onClose,onRetry}:{error:{target:string;action:string;message:string};station?:Station;onClose:()=>void;onRetry:()=>void}) { return <div className="scrim centered"><section className="command-error-modal"><div className="error-symbol"><AlertTriangle/></div><h2>操作执行失败</h2><p>{station?.name||"未知工站"} · {error.target} {error.action}</p><pre>{error.message}</pre><div><button onClick={onClose}>关闭</button><button className="primary" onClick={onRetry}><RotateCcw/>重试</button></div></section></div>; }

function Metric({icon, label, value, suffix, color}: {icon: React.ReactNode; label: string; value: string; suffix?: string; color: string}) { return <div className="metric"><div className={`metric-icon ${color}`}>{icon}</div><div><span>{label}</span><strong>{value}<small>{suffix}</small></strong></div></div>; }

function StationCard({station, color, onSelect, onTerminal, onCommand, busy}: {station: Station; color: string; onSelect: () => void; onTerminal: () => void; onCommand: (id:string,t:string,a:string)=>void; busy:string|null}) {
  const temperatureEntries = Object.entries(station.temperatures || {});
  const hot = temperatureEntries.map(([,value]) => value);
  const eefHot = temperatureEntries.filter(([name]) => name.startsWith("eef_motor_")).map(([,value]) => value);
  const maxTemp = hot.length ? Math.max(...hot) : undefined;
  const eefTemp = eefHot.length ? Math.max(...eefHot) : undefined;
  return <article className={`station-card ${station.online ? "" : "offline"}`} style={{"--accent": color} as React.CSSProperties}>
    <div className="card-head" onClick={onSelect}><div className="station-number">{station.name.match(/\d+/)?.[0] || "•"}</div><div><h3>{station.name}</h3><p>{station.ip}</p></div><span className={`connection `}>{station.online ? <Wifi size={14}/> : <WifiOff size={14}/>} {station.agent_online ? "监控在线" : station.ssh_reachable ? "SSH 在线" : "离线"}</span></div>
    <div className={`agent-status ${station.agent_online?"on":""}`}><span/><b>{station.agent_online?"监控已连接":"监控未连接"}</b><em>{station.deployment_message||"等待连接状态"}</em></div>
    <div className="process-row"><Process label="机械臂状态" state={station.robot_state}/><Process label="数据采集" state={station.collection_state}/></div>
    <div className="data-row"><div><Cpu/><span>CPU<strong>{pct(station.cpu_total)}</strong></span></div><div><Thermometer/><span>最高温度<strong>{maxTemp == null ? "—" : maxTemp.toFixed(1)+"°C"}</strong></span></div><div className="eef-temperature"><Thermometer/><span>夹爪温度<strong>{eefTemp == null ? "—" : eefTemp.toFixed(1)+"°C"}</strong></span></div></div>
    <div className="card-actions"><button disabled={!station.agent_online||!!busy} onClick={()=>onCommand(station.id,"robot",station.robot_state==="running"?"stop":"start")}>{station.robot_state==="running"?<CircleStop/>:<Play/>}机械臂{station.robot_state==="running"?"停止":"启动"}</button><button disabled={!station.agent_online||!!busy} onClick={()=>onCommand(station.id,"collection",station.collection_state==="running"?"stop":"start")}>{station.collection_state==="running"?<CircleStop/>:<Play/>}数采{station.collection_state==="running"?"停止":"启动"}</button><a className="card-link acquisition" href={acquisitionUrl(station.id)} target="_blank" rel="noreferrer"><Activity/>数采界面</a><button disabled={!station.online} onClick={onTerminal}><SquareTerminal/>文件 / SSH</button><button className="detail" onClick={onSelect}>详情<ChevronRight/></button></div>
    {station.active_alarm_count > 0 && <div className="alarm-ribbon"><AlertTriangle size={14}/>{station.active_alarm_count} 条活动告警</div>}
  </article>;
}

function Process({label, state}: {label:string; state:ProcessState}) { return <div><span>{label}</span><b className={state}><i/>{stateLabel[state] || state}</b></div>; }

function DetailDrawer({station, color, onClose, onEdit, onTerminal, onCommand, busy}: {station:Station;color:string;onClose:()=>void;onEdit:()=>void;onTerminal:()=>void;onCommand:(i:string,t:string,a:string)=>void;busy:string|null}) {
  const temperatureEntries = Object.entries(station.temperatures || {});
  const armTemperatures = temperatureEntries.filter(([name]) => !name.startsWith("eef_motor_"));
  const eefTemperatures = temperatureEntries.filter(([name]) => name.startsWith("eef_motor_"));
  return <div className="scrim" onMouseDown={e => e.target === e.currentTarget && onClose()}><aside className="drawer">
    <div className="drawer-head"><div className="station-number" style={{background:color}}>{station.name.match(/\d+/)?.[0] || "•"}</div><div><h2>{station.name}</h2><p>{station.ip} · SSH {station.ssh_reachable ? "在线" : "离线"} · {station.agent_online?"监控已连接":"监控未连接"}</p></div><button className="icon" title="编辑工站" onClick={onEdit}><Pencil/></button><button className="icon" onClick={onClose}><X/></button></div>
    <h4>程序控制</h4><div className="control-groups"><ControlGroup label="机械臂程序" state={station.robot_state} target="robot" station={station} onCommand={onCommand} busy={busy}/><ControlGroup label="数据采集程序" state={station.collection_state} target="collection" station={station} onCommand={onCommand} busy={busy}/></div>
    <div className="special-controls"><button disabled={!station.agent_online||station.robot_state!=="running"||!!busy} onClick={()=>onCommand(station.id,"robot_zero","start")}><RotateCcw/>机械臂归零</button><button disabled={!station.agent_online||station.robot_state!=="running"||!!busy} onClick={()=>onCommand(station.id,"state_reset","start")}><RefreshCw/>状态机复位</button></div>
    <div className="external-tools"><a href={acquisitionUrl(station.id)} target="_blank" rel="noreferrer"><Activity/>打开数据采集界面</a><a href={fsmUrl(station)} target="_blank" rel="noreferrer"><Gauge/>状态机监控</a></div><button className="open-terminal" onClick={onTerminal}><SquareTerminal/>打开文件管理 / 多会话 SSH 终端</button>
    <RemoteTerminal station={station}/>
    <div className="detail-title"><h4>关节角度</h4><small>弧度 / 角度</small></div><div className="joint-list">{Object.entries(station.joints || {}).map(([name,value]) => <div key={name}><span>{name}</span><strong>{value.toFixed(3)} rad</strong><em>{(value*180/Math.PI).toFixed(1)}°</em></div>)}{!Object.keys(station.joints || {}).length && <p className="muted">尚未收到 ROS2 关节数据</p>}</div>
    <div className="detail-title"><h4>关节温度</h4><small>摄氏度</small></div><div className="temp-grid">{armTemperatures.map(([name,value]) => <div key={name}><Thermometer/><span>{temperatureLabel(name)}<strong>{value.toFixed(1)}°C</strong></span></div>)}{!armTemperatures.length && <p className="muted">尚未收到关节温度</p>}</div>
    <div className="detail-title"><h4>夹爪温度</h4><small>EEF 电机 · 摄氏度</small></div><div className="temp-grid eef-temp-grid">{eefTemperatures.map(([name,value]) => <div key={name}><Thermometer/><span>{temperatureLabel(name)}<strong>{value.toFixed(1)}°C</strong></span></div>)}{!eefTemperatures.length && <p className="muted">尚未收到夹爪温度</p>}</div>
    <div className="detail-title"><h4>CPU 占用</h4><small>与 htop 同源</small></div><div className="cpu-bars">{[["整机",station.cpu_total],["Agent",station.cpu_agent],["机械臂",station.cpu_robot],["数采",station.cpu_collection],...((station.cpu_per_core||[]).map((value,index)=>[`CPU ${index}`,value]))].map(([name,val]) => <div key={String(name)}><label>{name}<b>{pct(val as number)}</b></label><span><i style={{width:`${Math.min(Number(val)||0,100)}%`}}/></span></div>)}</div>
  </aside></div>;
}

function ControlGroup({label,state,target,station,onCommand,busy}:{label:string;state:ProcessState;target:string;station:Station;onCommand:(i:string,t:string,a:string)=>void;busy:string|null}) { const disabled=!station.agent_online||!!busy; return <div><header><span>{label}</span><Process label="" state={state}/></header><div><button disabled={disabled} onClick={()=>onCommand(station.id,target,"start")}><Play/>开</button><button disabled={disabled} onClick={()=>onCommand(station.id,target,"stop")}><CircleStop/>关</button>{target==="collection"?<button className="danger" disabled={disabled||state!=="running"} onClick={()=>onCommand(station.id,"collection_service","stop")}><CircleStop/>停止</button>:<button disabled={disabled} onClick={()=>onCommand(station.id,target,"restart")}><RotateCcw/>重启</button>}</div></div>; }

function RemoteTerminal({station}:{station:Station}) {
  const [command,setCommand]=useState("");
  const [output,setOutput]=useState("输入单条命令，执行结果会显示在这里。\n");
  const [running,setRunning]=useState(false);
  async function execute(e:React.FormEvent){
    e.preventDefault();
    if(!command.trim()||!confirm(`确认以工站权限执行命令？\n\n${command}`)) return;
    setRunning(true); setOutput(`$ ${command}\n执行中…`);
    try {
      const job=await api<{id:string}>(`/api/stations/${station.id}/commands`,{method:"POST",body:JSON.stringify({target:"shell",action:"run",command})});
      for(let attempt=0;attempt<120;attempt++){
        await new Promise(resolve=>setTimeout(resolve,250));
        const jobs=await api<Array<{id:string;status:string;result?:string}>>("/api/commands?limit=100");
        const current=jobs.find(item=>item.id===job.id);
        if(current&&["completed","failed"].includes(current.status)){setOutput(`$ ${command}\n${current.result||current.status}`);return;}
      }
      setOutput(`$ ${command}\n等待结果超时`);
    } catch(error){setOutput(`$ ${command}\n${(error as Error).message}`);}
    finally{setRunning(false);}
  }
  return <section className="terminal"><header><span>远程命令</span><small>单条命令 · 30 秒超时</small></header><pre>{output}</pre><form onSubmit={execute}><span>$</span><input value={command} onChange={e=>setCommand(e.target.value)} disabled={!station.online||running} placeholder="例如：systemctl status airbot-robot --no-pager"/><button disabled={!station.online||running}>{running?<Loader2 className="spin"/>:"执行"}</button></form></section>;
}

type TerminalCredentials = {username:string;password:string;port:number};

function TerminalWindow({station,onClose}:{station:Station;onClose:()=>void}) {
  const [credentials,setCredentials]=useState<TerminalCredentials>({username:"root",password:"",port:22});
  const [authorized,setAuthorized]=useState(false);
  const [tabs,setTabs]=useState([{id:crypto.randomUUID(),title:"终端 1"}]);
  const [active,setActive]=useState(tabs[0].id);
  function addTab(){const id=crypto.randomUUID();setTabs(current=>[...current,{id,title:`终端 ${current.length+1}`}]);setActive(id);}
  function closeTab(id:string){setTabs(current=>{const next=current.filter(tab=>tab.id!==id);if(!next.length){onClose();return current}if(active===id)setActive(next[0].id);return next;});}
  if(!authorized) return <div className="terminal-scrim"><form className="terminal-login" onSubmit={e=>{e.preventDefault();setAuthorized(true)}}><header><SquareTerminal/><div><h2>连接 {station.name}</h2><p>{station.ip} · 密码仅保存在当前窗口内存</p></div><button type="button" onClick={onClose}><X/></button></header><label>SSH 账号<input required value={credentials.username} onChange={e=>setCredentials({...credentials,username:e.target.value})}/></label><label>SSH 密码<input required autoFocus type="password" value={credentials.password} onChange={e=>setCredentials({...credentials,password:e.target.value})}/></label><label>SSH 端口<input required type="number" min="1" max="65535" value={credentials.port} onChange={e=>setCredentials({...credentials,port:Number(e.target.value)})}/></label><button className="terminal-connect">SSH 连接并打开终端</button></form></div>;
  return <div className="terminal-scrim"><section className="terminal-window"><header><div><SquareTerminal/><strong>{station.name}</strong><span>{station.ip}</span></div><button onClick={addTab}><Plus/>新建终端</button><button onClick={onClose}><X/></button></header><nav>{tabs.map(tab=><button className={active===tab.id?"active":""} onClick={()=>setActive(tab.id)} key={tab.id}><span>{tab.title}</span><X onClick={e=>{e.stopPropagation();closeTab(tab.id)}}/></button>)}</nav><div className="terminal-panes">{tabs.map(tab=><TerminalPane key={tab.id} station={station} credentials={credentials} active={active===tab.id}/>)}</div></section></div>;
}

type RemoteFile = {name:string;path:string;is_dir:boolean;size:number;mtime?:number};

function TerminalPane({station,credentials,active}:{station:Station;credentials:TerminalCredentials;active:boolean}) {
  const container=useRef<HTMLDivElement>(null), socketRef=useRef<WebSocket|null>(null), uploadRef=useRef<HTMLInputElement>(null);
  const fitRef=useRef<FitAddon|null>(null), pendingMode=useRef<"download"|"preview">("download");
  const [remotePath,setRemotePath]=useState("."), [files,setRemoteFiles]=useState<RemoteFile[]>([]), [fileBusy,setFileBusy]=useState(false);
  const [preview,setPreview]=useState<{name:string;url?:string;text?:string}|null>(null);
  function send(payload:object){const socket=socketRef.current;if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(payload));}
  function list(path:string){setFileBusy(true);send({type:"file_list",path});}
  function parentPath(path:string){if(path==="."||path==="/")return path;const clean=path.replace(/\/$/,""),index=clean.lastIndexOf("/");return index<=0?"/":clean.slice(0,index);}
  function requestFile(file:RemoteFile,mode:"download"|"preview"){if(file.is_dir){setRemotePath(file.path);list(file.path);return}pendingMode.current=mode;setFileBusy(true);send({type:"file_download",path:file.path});}
  function receiveFile(name:string,data:string){const raw=atob(data),content=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)content[i]=raw.charCodeAt(i);const ext=name.split(".").pop()?.toLowerCase()||"";if(pendingMode.current==="preview"){if(["png","jpg","jpeg","gif","webp","svg"].includes(ext)){const type=ext==="svg"?"image/svg+xml":`image/${ext==="jpg"?"jpeg":ext}`;setPreview({name,url:URL.createObjectURL(new Blob([content],{type}))})}else if(["txt","log","json","yaml","yml","toml","ini","conf","cfg","xml","csv","md","py","sh","js","ts","tsx","css","html"].includes(ext)){setPreview({name,text:new TextDecoder().decode(content)})}else setPreview({name,text:"该文件为二进制格式，请下载后查看。"})}else{const url=URL.createObjectURL(new Blob([content])),link=document.createElement("a");link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}setFileBusy(false)}
  function upload(file?:File){if(!file)return;if(file.size>20*1024*1024){alert("单个上传文件不能超过 20 MB");return}setFileBusy(true);const reader=new FileReader();reader.onload=()=>send({type:"file_upload",path:remotePath,name:file.name,data:String(reader.result).split(",")[1]||""});reader.onerror=()=>setFileBusy(false);reader.readAsDataURL(file)}
  useEffect(()=>{
    if(!container.current)return;let disposed=false,retryTimer:number|undefined,retryDelay=1000;
    const terminal=new XTerm({cursorBlink:true,fontSize:13,fontFamily:"ui-monospace, SFMono-Regular, Menlo, monospace",theme:{background:"#0b0f16",foreground:"#d7dee9",cursor:"#7aa2f7",selectionBackground:"#334155"},scrollback:5000});
    const fit=new FitAddon();fitRef.current=fit;terminal.loadAddon(fit);terminal.open(container.current);fit.fit();
    const connect=()=>{if(disposed)return;terminal.writeln("\r\n\x1b[36m正在建立 SSH 连接…\x1b[0m");const socket=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws/terminal/${station.id}`);socket.binaryType="arraybuffer";socketRef.current=socket;socket.onopen=()=>socket.send(JSON.stringify({...credentials,cols:terminal.cols,rows:terminal.rows}));socket.onmessage=event=>{if(typeof event.data!=="string"){terminal.write(new Uint8Array(event.data));return}const message=JSON.parse(event.data);if(message.type==="error"){terminal.writeln(`\r\n\x1b[31m${message.message}\x1b[0m`);setFileBusy(false)}else if(message.type==="connected"){terminal.writeln("\r\n\x1b[32mSSH 已连接\x1b[0m");retryDelay=1000;setFileBusy(true);socket.send(JSON.stringify({type:"file_list",path:"."}))}else if(message.type==="file_list"){setRemotePath(message.path);setRemoteFiles(message.entries);setFileBusy(false)}else if(message.type==="file_uploaded"){socket.send(JSON.stringify({type:"file_list",path:message.path}))}else if(message.type==="file_download")receiveFile(message.name,message.data)};socket.onclose=()=>{if(disposed)return;terminal.writeln(`\r\n\x1b[33mSSH 已断开，${Math.round(retryDelay/1000)} 秒后自动重连…\x1b[0m`);retryTimer=window.setTimeout(connect,retryDelay);retryDelay=Math.min(retryDelay*2,15000)}};connect();
    const input=terminal.onData(data=>{const socket=socketRef.current;if(socket?.readyState===WebSocket.OPEN)socket.send(new TextEncoder().encode(data))});
    const observer=new ResizeObserver(()=>{if(!active)return;fit.fit();const socket=socketRef.current;if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:"resize",cols:terminal.cols,rows:terminal.rows}))});observer.observe(container.current);
    return()=>{disposed=true;window.clearTimeout(retryTimer);observer.disconnect();input.dispose();socketRef.current?.close();socketRef.current=null;terminal.dispose()};
  },[station.id,credentials]);
  useEffect(()=>{if(active)setTimeout(()=>fitRef.current?.fit(),20)},[active]);
  return <div className={`terminal-pane ${active?"active":""}`}><div className="terminal-layout"><div className="xterm-host" ref={container}/><aside className="remote-files"><header><div><Folder/><strong>远端文件</strong></div><button disabled={fileBusy} onClick={()=>uploadRef.current?.click()}><Upload/>上传</button><input ref={uploadRef} hidden type="file" onChange={e=>{upload(e.target.files?.[0]);e.target.value=""}}/></header><div className="remote-path"><button disabled={fileBusy} onClick={()=>{const path=parentPath(remotePath);setRemotePath(path);list(path)}}>↑</button><input value={remotePath} onChange={e=>setRemotePath(e.target.value)} onKeyDown={e=>e.key==="Enter"&&list(remotePath)}/><button disabled={fileBusy} onClick={()=>list(remotePath)}>{fileBusy?<Loader2 className="spin"/>:<RefreshCw/>}</button></div><div className="remote-list">{files.map(file=><div key={file.path} onDoubleClick={()=>requestFile(file,"preview")}><span onClick={()=>requestFile(file,"preview")}>{file.is_dir?<Folder/>:<FileText/>}<b>{file.name}</b><small>{file.is_dir?"目录":bytes(file.size)}</small></span>{!file.is_dir&&<button title="下载" onClick={()=>requestFile(file,"download")}><Download/></button>}</div>)}{!files.length&&!fileBusy&&<p>目录为空</p>}</div></aside></div>{preview&&<div className="file-preview"><header><strong>{preview.name}</strong><button onClick={()=>{if(preview.url)URL.revokeObjectURL(preview.url);setPreview(null)}}><X/></button></header>{preview.url?<img src={preview.url}/>:<pre>{preview.text}</pre>}</div>}</div>;
}

function LogView({stations,selected,setSelected,logs,sourceGroup,setSourceGroup,errorsOnly,setErrorsOnly,files,page,pages,total,onPage,onDelete,onClear,clearing}:{stations:Station[];selected:string|null;setSelected:(v:string|null)=>void;logs:LogEntry[];sourceGroup:"all"|"robot"|"collection";setSourceGroup:(value:"all"|"robot"|"collection")=>void;errorsOnly:boolean;setErrorsOnly:(v:boolean)=>void;files:LogFile[];page:number;pages:number;total:number;onPage:(page:number)=>void;onDelete:(paths:string[])=>Promise<void>;onClear:()=>Promise<void>;clearing:boolean}) {
  const [checked,setChecked]=useState<string[]>([]);
  const [deleting,setDeleting]=useState(false);
  const visibleFiles=useMemo(()=>files.filter(file=>(!selected||file.station_id===selected)&&matchesLogSource(file.source,sourceGroup)),[files,selected,sourceGroup]);
  useEffect(()=>setChecked(current=>current.filter(path=>visibleFiles.some(file=>file.path===path))),[visibleFiles]);
  const allChecked=visibleFiles.length>0&&visibleFiles.every(file=>checked.includes(file.path));
  function toggle(path:string){setChecked(current=>current.includes(path)?current.filter(item=>item!==path):[...current,path]);}
  async function removeChecked(){if(!checked.length)return;setDeleting(true);try{await onDelete(checked);setChecked([]);}finally{setDeleting(false);}}
  return <div className="panel log-panel">
    <div className="log-station-switcher"><button className={!selected?"active":""} onClick={()=>setSelected(null)}>全部工站</button>{stations.map(station=><button className={selected===station.id?"active":""} onClick={()=>setSelected(station.id)} key={station.id}><span className={station.agent_online?"online":""}/>{station.name}</button>)}</div>
    <div className="toolbar"><div className="log-source-filter"><button className={sourceGroup==="all"?"active":""} onClick={()=>setSourceGroup("all")}>全部日志</button><button className={sourceGroup==="robot"?"active":""} onClick={()=>setSourceGroup("robot")}><Bot/>机械臂控制</button><button className={sourceGroup==="collection"?"active":""} onClick={()=>setSourceGroup("collection")}><Activity/>数据采集</button></div><label className="switch"><input type="checkbox" checked={errorsOnly} onChange={e=>setErrorsOnly(e.target.checked)}/><i/>仅查看 ERROR / FATAL</label><button className="danger" disabled={!total||clearing} onClick={onClear}>{clearing?<Loader2 className="spin"/>:<Trash2/>}一键清空今日日志</button><span>今日共 {total} 条 · 当前：{selected?stations.find(station=>station.id===selected)?.name:"全部工站"}</span></div>
    <div className="log-stream">{logs.map((item,i)=><div className={"log-line "+item.level.toLowerCase()} key={[item.station_id,item.sequence,i].join("-")}><time>{formatTime(item.timestamp)}</time><b>{stations.find(station=>station.id===item.station_id)?.name||item.station_id.slice(0,8)}</b><em>{sourceLabel(item.source)}</em><strong>{item.level}</strong><p>{item.message}</p></div>)}{logs.length===0&&<div className="empty"><FileText/><p>{errorsOnly?"没有错误日志":"等待实时日志…"}</p></div>}</div>
    <div className="pagination"><button disabled={page<=1} onClick={()=>onPage(page-1)}>上一页</button><span>第 {page} / {pages} 页</span><button disabled={page>=pages} onClick={()=>onPage(page+1)}>下一页</button></div>
    <div className="file-list"><div className="file-list-head"><h3>日志文件下载</h3><div><label className="file-check"><input type="checkbox" checked={allChecked} disabled={!visibleFiles.length} onChange={()=>setChecked(allChecked?[]:visibleFiles.map(file=>file.path))}/>全选当前工站</label><button className="danger" disabled={!checked.length||deleting} onClick={removeChecked}>{deleting?<Loader2 className="spin"/>:<Trash2/>}批量删除 ({checked.length})</button></div></div>{stations.filter(station=>(!selected||station.id===selected)&&visibleFiles.some(file=>file.station_id===station.id)).map(station=><section className="station-file-group" key={station.id}><header><div><strong>{station.name}</strong><small>{station.ip}</small></div><span>{visibleFiles.filter(file=>file.station_id===station.id).length} 个文件</span></header>{visibleFiles.filter(file=>file.station_id===station.id).map(file=><div className="station-file" key={file.path}><label className="file-check"><input type="checkbox" checked={checked.includes(file.path)} onChange={()=>toggle(file.path)}/></label><FileText/><span><strong>{sourceLabel(file.source)}</strong><small>{file.log_date} · {bytes(file.bytes)}</small></span><a href={"/api/logs/download?path="+encodeURIComponent(file.path)}><Download/>下载</a></div>)}</section>)}{visibleFiles.length===0&&<div className="empty"><FileText/><p>暂无可下载日志</p></div>}</div>
  </div>;
}

function FsmMonitor({stations}:{stations:Station[]}) {
  const [selected,setSelected]=useState(stations[0]?.id||"");
  useEffect(()=>{if(!stations.some(station=>station.id===selected))setSelected(stations[0]?.id||"")},[stations,selected]);
  const station=stations.find(item=>item.id===selected);
  return <section className="fsm-monitor"><header><div><h2>状态机实时监控</h2><p>每秒读取状态与场景，并持续显示设备遥测。</p></div><select value={selected} onChange={event=>setSelected(event.target.value)}>{stations.map(item=><option key={item.id} value={item.id}>{item.name} · {item.ip}</option>)}</select>{station&&<a href={fsmUrl(station)} target="_blank" rel="noreferrer">新窗口打开</a>}</header>{station?<iframe key={station.id} title={station.name+" 状态机"} src={fsmUrl(station)}/>:<div className="empty"><Gauge/><p>暂无工站</p></div>}</section>;
}

function AlarmView({alarms,stations,onAck,onAckAll,onDelete}:{alarms:Alarm[];stations:Station[];onAck:(id:string)=>void;onAckAll:()=>Promise<void>;onDelete:(ids:string[])=>Promise<boolean>}) {
  const [checked,setChecked]=useState<string[]>([]);
  const [deleting,setDeleting]=useState(false);
  useEffect(()=>setChecked(current=>current.filter(id=>alarms.some(alarm=>alarm.id===id))),[alarms]);
  const allChecked=alarms.length>0&&alarms.every(alarm=>checked.includes(alarm.id));
  const activeCount=alarms.filter(alarm=>alarm.status==="active").length;
  const groups=useMemo(()=>{
    const stationById=new Map(stations.map(station=>[station.id,station]));
    const alarmsByStation=new Map<string,Alarm[]>();
    alarms.forEach(alarm=>alarmsByStation.set(alarm.station_id,[...(alarmsByStation.get(alarm.station_id)||[]),alarm]));
    return [...alarmsByStation.entries()].map(([stationId,items])=>({station:stationById.get(stationId),stationId,items})).sort((a,b)=>{
      const aIndex=stations.findIndex(station=>station.id===a.stationId);
      const bIndex=stations.findIndex(station=>station.id===b.stationId);
      return (aIndex<0?Number.MAX_SAFE_INTEGER:aIndex)-(bIndex<0?Number.MAX_SAFE_INTEGER:bIndex);
    });
  },[alarms,stations]);
  function toggle(id:string){setChecked(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id]);}
  function toggleGroup(items:Alarm[]){const ids=items.map(alarm=>alarm.id);const selected=ids.every(id=>checked.includes(id));setChecked(current=>selected?current.filter(id=>!ids.includes(id)):[...new Set([...current,...ids])]);}
  async function removeChecked(){if(!checked.length)return;setDeleting(true);try{if(await onDelete(checked))setChecked([]);}finally{setDeleting(false);}}
  return <div className="panel alarm-list"><div className="alarm-actions"><button className="primary" disabled={!activeCount} onClick={onAckAll}><CheckCircle2/>一键已读 ({activeCount})</button><span/><label className="alarm-check"><input type="checkbox" checked={allChecked} disabled={!alarms.length} onChange={()=>setChecked(allChecked?[]:alarms.map(alarm=>alarm.id))}/>全选</label><button className="danger" disabled={!checked.length||deleting} onClick={removeChecked}>{deleting?<Loader2 className="spin"/>:<Trash2/>}批量删除 ({checked.length})</button></div>{groups.map(({station,stationId,items})=>{const groupChecked=items.every(alarm=>checked.includes(alarm.id));const groupActive=items.filter(alarm=>alarm.status==="active").length;return <section className="station-alarm-group" key={stationId}><header><label className="alarm-check"><input type="checkbox" checked={groupChecked} onChange={()=>toggleGroup(items)}/></label><div><strong>{station?.name||"未知工站"}</strong><small>{station?.ip||stationId}</small></div><span>{items.length} 条 · 活动 {groupActive} 条</span></header>{items.map(alarm=><div key={alarm.id} className={"alarm-row "+alarm.severity+" "+alarm.status}><label className="alarm-check alarm-select"><input type="checkbox" checked={checked.includes(alarm.id)} onChange={()=>toggle(alarm.id)}/></label><div className="alarm-icon"><AlertTriangle/></div><div><header><span>{alarm.severity==="critical"?"严重":"警告"}</span><time>{formatTime(alarm.last_at)}</time></header><p>{alarm.message}</p><small>首次发生：{formatTime(alarm.first_at)} · 状态：{alarm.status}</small></div>{alarm.status==="active"&&<button onClick={()=>onAck(alarm.id)}>标为已读</button>}</div>)}</section>})}{alarms.length===0&&<div className="empty"><CheckCircle2/><p>当前没有告警</p></div>}</div>;
}

function EditStation({station,onClose,onSaved,onDeleted}:{station:Station;onClose:()=>void;onSaved:()=>void;onDeleted:()=>void}) {
  const [form,setForm]=useState({name:station.name,ip:station.ip,ssh_username:station.ssh_username||"root",ssh_port:station.ssh_port||22,password:"",ros_domain_id:station.ros_domain_id||0,joint_topic:station.joint_topic||"/joint_states",temperature_topics:(station.temperature_topics||[]).join(", "),notes:station.notes||"",acquisition_project_id:station.acquisition_project_id ? String(station.acquisition_project_id) : ""});
  const [saving,setSaving]=useState(false);const [error,setError]=useState("");
  async function submit(e:React.FormEvent){e.preventDefault();setSaving(true);setError("");try{const {password,...values}=form;await api(`/api/stations/${station.id}`,{method:"PATCH",body:JSON.stringify({...values,acquisition_project_id:form.acquisition_project_id ? Number(form.acquisition_project_id) : undefined,temperature_topics:form.temperature_topics.split(",").map(value=>value.trim()).filter(Boolean)})});if(password)await api(`/api/stations/${station.id}/reconnect`,{method:"POST",body:JSON.stringify({username:form.ssh_username,password,ssh_port:form.ssh_port,accept_host_key:true})});onSaved();}catch(exc){setError((exc as Error).message)}finally{setSaving(false)}}
  async function remove(){if(!confirm(`确认删除“${station.name}”吗？该工站连接会立即断开。`))return;setSaving(true);setError("");try{await api(`/api/stations/${station.id}`,{method:"DELETE"});onDeleted();}catch(exc){setError((exc as Error).message);setSaving(false)}}
  return <div className="scrim"><form className="modal" onSubmit={submit}><div className="modal-head"><div><h2>编辑工站</h2><p>填写密码会在保存后立即重新 SSH 验证；密码不会保存</p></div><button type="button" className="icon" onClick={onClose}><X/></button></div><div className="two"><label>工站名称<input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label><label>IP 地址<input required value={form.ip} onChange={e=>setForm({...form,ip:e.target.value})}/></label></div><div className="two"><label>SSH 账号<input required value={form.ssh_username} onChange={e=>setForm({...form,ssh_username:e.target.value})}/></label><label>SSH 端口<input type="number" min="1" max="65535" value={form.ssh_port} onChange={e=>setForm({...form,ssh_port:Number(e.target.value)})}/></label></div><label>SSH 密码（可选）<input type="password" value={form.password} onChange={e=>setForm({...form,password:e.target.value})} placeholder="填写后保存并重新连接"/></label><div className="two"><label>ROS Domain ID<input type="number" min="0" max="232" value={form.ros_domain_id} onChange={e=>setForm({...form,ros_domain_id:Number(e.target.value)})}/></label><label>关节状态话题<input required value={form.joint_topic} onChange={e=>setForm({...form,joint_topic:e.target.value})}/></label></div><label>温度话题（逗号分隔）<input value={form.temperature_topics} onChange={e=>setForm({...form,temperature_topics:e.target.value})}/></label><label>数据采集项目 ID<input type="number" min="1" placeholder="例如：184" value={form.acquisition_project_id} onChange={e=>setForm({...form,acquisition_project_id:e.target.value})}/></label><label>备注<input value={form.notes} onChange={e=>setForm({...form,notes:e.target.value})}/></label>{error&&<p className="form-error">{error}</p>}<div className="modal-actions split"><button type="button" className="danger" disabled={saving} onClick={remove}><Trash2/>删除工站</button><span/><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={saving}>{saving?<Loader2 className="spin"/>:<CheckCircle2/>}{form.password?"保存并重新 SSH":"保存修改"}</button></div></form></div>;
}

function AddStation({onClose,onAdded}:{onClose:()=>void;onAdded:()=>void}) {
  const [form,setForm]=useState({name:"工站 1",ip:"",ssh_port:22,username:"root",password:"",ros_domain_id:0,joint_topic:"/joint_states",temperature_topics:"",notes:"",acquisition_project_id:"",accept_host_key:true});
  const [submitting,setSubmitting]=useState(false);
  const [error,setError]=useState("");
  async function submit(e:React.FormEvent){
    e.preventDefault(); setSubmitting(true); setError("");
    try {
      await api("/api/stations/onboard",{method:"POST",body:JSON.stringify({...form,acquisition_project_id:form.acquisition_project_id ? Number(form.acquisition_project_id) : null,temperature_topics:form.temperature_topics.split(",").map(v=>v.trim()).filter(Boolean)})});
      onAdded();
    } catch(ex) { setError((ex as Error).message); }
    finally { setSubmitting(false); }
  }
  return <div className="scrim"><form className="modal" onSubmit={submit}>
    <div className="modal-head"><div><h2>添加新工站</h2><p>提交后立即通过 SSH 自动检查、安装并连接；密码不会保存</p></div><button type="button" className="icon" onClick={onClose}><X/></button></div>
    <div className="two"><label>工站名称<input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label><label>IP 地址<input required placeholder="192.168.1.101" value={form.ip} onChange={e=>setForm({...form,ip:e.target.value})}/></label></div>
    <div className="two"><label>SSH 账号<input required value={form.username} onChange={e=>setForm({...form,username:e.target.value})}/></label><label>SSH 端口<input required type="number" min="1" max="65535" value={form.ssh_port} onChange={e=>setForm({...form,ssh_port:Number(e.target.value)})}/></label></div>
    <label>首次连接密码<input required type="password" value={form.password} onChange={e=>setForm({...form,password:e.target.value})}/></label>
    <div className="two"><label>ROS Domain ID<input type="number" min="0" max="232" value={form.ros_domain_id} onChange={e=>setForm({...form,ros_domain_id:Number(e.target.value)})}/></label><label>关节状态话题<input required value={form.joint_topic} onChange={e=>setForm({...form,joint_topic:e.target.value})}/></label></div>
    <label>温度话题（逗号分隔）<input placeholder="/joint1/temperature, /diagnostics|diagnostic" value={form.temperature_topics} onChange={e=>setForm({...form,temperature_topics:e.target.value})}/></label>
    <label>数据采集项目 ID（可选）<input type="number" min="1" placeholder="例如：184；保存后自动适配数采界面" value={form.acquisition_project_id} onChange={e=>setForm({...form,acquisition_project_id:e.target.value})}/></label>
    <label>备注<input placeholder="例如：左侧产线、机械臂序列号" value={form.notes} onChange={e=>setForm({...form,notes:e.target.value})}/></label>
    <label className="check"><input type="checkbox" checked={form.accept_host_key} onChange={e=>setForm({...form,accept_host_key:e.target.checked})}/>首次连接时接受 SSH 主机指纹</label>
    {error&&<p className="form-error">{error}</p>}
    <div className="modal-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={submitting}>{submitting?<Loader2 className="spin"/>:<Plus/>}{submitting?"正在 SSH 连接…":"安装并连接"}</button></div>
  </form></div>;
}
