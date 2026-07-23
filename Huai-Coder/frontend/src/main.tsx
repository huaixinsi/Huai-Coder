import React, { FormEvent, StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./style.css";

type StreamEvent = {
  type: string;
  content?: string;
  tool?: string;
  run_id?: number;
  plan_id?: number;
  approval_id?: number;
  risk_level?: string;
  arguments?: string;
  target_path?: string;
};
type PlanSummary = { id: number; goal?: string; summary?: string; status: string };
type PlanTask = { id: number; task_key: string; title: string; description: string; task_type: string; status: string; output_data?: string; error_message?: string; retry_count: number };
type Project = { id: number; name: string; description?: string };
type ChatSession = { id: number; project_id: number; title: string };
type Memory = { id: number; memory_type: string; content: string; importance: number; confidence: number; status: string; source_session_id?: number; expires_at?: string };
type MemoryOverview = { session: Memory[]; project: Memory[]; user: Memory[] };
type MemoryAudit = { id: number; memory_id: number; action: string; before_content?: string | null; after_content?: string | null; reason: string; created_at?: string };
type SubAgent = { name: string; description: string; tools: string[]; needs_approval: boolean };
type SubAgentCatalog = { graph: { type: string; root: string; child: string; edges: string[] }; agents: SubAgent[] };
type Activity = { type: string; tool?: string; arguments?: string; content?: string; status: "running" | "done" | "info" | "error" };
type ChatMessage = { id: string; role: "user" | "assistant" | "system"; content: string; activities: Activity[]; status?: "running" | "done" | "failed" };
type Approval = StreamEvent;
type DirectoryFile = { file: File; relativePath: string };
type ClientToolCall = { invocation_id: string; tool: string; arguments: Record<string, unknown>; execution: "client" };
type ClientToolResult = { ok: boolean; result?: string; error_type?: string; changed_paths?: string[]; content_hash?: string };
type WorkspaceStatus = { bound: boolean; file_count: number; folder_name?: string | null };
type McpTool = { name: string; original_name: string; description?: string; risk_level: string; risk_reason?: string; requires_approval: boolean };
type McpServer = { id: string; status: string; enabled: boolean; transport: string; tool_count: number; error?: string | null; tools?: McpTool[] };
type RunStatus = "idle" | "running" | "waiting" | "completed" | "failed" | "limited" | "stopped";

declare global {
  interface Window { showDirectoryPicker?: (options?: { mode?: "read" | "readwrite" }) => Promise<FileSystemDirectoryHandle>; }
  interface FileSystemDirectoryHandle { values(): AsyncIterableIterator<FileSystemFileHandle | FileSystemDirectoryHandle>; }
}

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const RUNNER_URL = import.meta.env.VITE_RUNNER_URL ?? "http://127.0.0.1:8765";

function Markdown({ value }: { value: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{value}</ReactMarkdown>;
}

function ActivityList({ activities }: { activities: Activity[] }) {
  if (!activities.length) return null;
  const toolCalls = activities.filter(activity => activity.type === "tool" || activity.type === "agent");
  const finished = toolCalls.filter(activity => activity.status !== "running").length;
  const formatArguments = (value?: string) => {
    if (!value) return "";
    try { return JSON.stringify(JSON.parse(value).arguments ?? JSON.parse(value), null, 2); } catch { return value; }
  };
  return <details className="execution-panel" open={activities.some(activity => activity.status === "running")}>
    <summary><span className="activity-dot" />执行过程 - {toolCalls.length} 次工具调用<span className="activity-status">已完成 {finished}/{toolCalls.length}</span></summary>
    <div className="activity-list">{activities.map((activity, index) => activity.type === "context.compacted" || activity.type === "guard" ? <div className={activity.type === "guard" ? "repeat-note" : "context-note"} key={`${activity.type}-${index}`}>{activity.type === "guard" ? "重复调用防护：" : "上下文已压缩："}{activity.content}</div> : <details className={`activity ${activity.status}`} key={`${activity.type}-${activity.tool ?? ""}-${index}`}>
      <summary><span className="activity-dot" />{index + 1}. {activity.tool ?? "工具调用"}<span className="activity-status">{activity.status === "running" ? "进行中" : activity.status === "error" ? "失败" : "已完成"}</span></summary>
      <div className="activity-body">
        {activity.arguments && <><label>参数</label><pre>{formatArguments(activity.arguments)}</pre></>}
        {activity.content && <><label>结果</label><pre>{activity.content}</pre></>}
      </div>
    </details>)}</div>
  </details>;
}

function McpDock({
  servers,
  busy,
  error,
  onRefresh,
  onConnect,
  onDisconnect,
  onReconnect,
  expandedServer,
  toolCache,
  onToggleTools,
}: {
  servers: McpServer[];
  busy: boolean;
  error: string;
  onRefresh: () => void;
  onConnect: (id: string) => void;
  onDisconnect: (id: string) => void;
  onReconnect: (id: string) => void;
  expandedServer: string | null;
  toolCache: Record<string, McpTool[]>;
  onToggleTools: (id: string) => void;
}) {
  return <aside className="mcp-dock panel" aria-label="MCP 服务">
    <div className="section-heading"><div><h3>MCP 工具</h3><p>浏览器、GitHub 等外部能力</p></div><button type="button" className="secondary-button" onClick={onRefresh} disabled={busy}>刷新</button></div>
    {servers.length === 0 ? <p className="muted">未配置 MCP Server。复制 backend/mcp.example.json 后设置 MCP_CONFIG_PATH。</p> : <ul className="mcp-list">{servers.map(server => <li key={server.id}><div className="mcp-server-main"><div className="mcp-server-row"><strong>{server.id}</strong><span className={`mcp-status ${server.status}`}>{server.status}</span></div><small>{server.transport} · {server.tool_count} 个工具</small>{server.error && <span className="folder-error">{server.error}</span>}{(server.status === "ready" || server.tool_count > 0) && <button type="button" className="mcp-tools-toggle" onClick={() => onToggleTools(server.id)}>{expandedServer === server.id ? "收起工具" : "查看工具"}</button>}{expandedServer === server.id && <ul className="mcp-tool-list">{(toolCache[server.id] ?? server.tools ?? []).length === 0 ? <li className="muted">暂无已发现工具</li> : (toolCache[server.id] ?? server.tools ?? []).map(tool => <li key={tool.name}><strong>{tool.original_name}</strong><small>{tool.risk_level}{tool.requires_approval ? " · 需要审批" : " · 可直接调用"}</small>{tool.description && <span>{tool.description}</span>}</li>)}</ul>}</div>{server.status === "ready" ? <div className="mcp-actions"><button type="button" className="secondary-button" onClick={() => onReconnect(server.id)} disabled={busy}>重连</button><button type="button" className="delete-button" onClick={() => onDisconnect(server.id)} disabled={busy}>断开</button></div> : <button type="button" className="delete-button" onClick={() => onConnect(server.id)} disabled={busy || !server.enabled}>连接</button>}</li>)}</ul>}
    {error && <p className="folder-error">{error}</p>}
  </aside>;
}

function SubAgentDock({ catalog }: { catalog: SubAgentCatalog | null }) {
  if (!catalog) return null;
  return <aside className="subagent-dock panel" aria-label="SubAgent 拓扑和权限">
    <div className="section-heading"><div><h3>SubAgent 拓扑</h3><p>{catalog.graph.root} → {catalog.graph.child}，运行详情显示在对话执行流中</p></div><span className="status-pill">{catalog.agents.length} 个角色</span></div>
    <div className="subagent-graph">{catalog.graph.edges.map(edge => <span key={edge}>{edge}</span>)}</div>
    <ul className="subagent-list">{catalog.agents.map(agent => <li key={agent.name}><div><strong>{agent.name}</strong><small>{agent.description}</small><small>工具：{agent.tools.join("、")} · {agent.needs_approval ? "需要审批" : "只读"}</small></div></li>)}</ul>
  </aside>;
}

function MemoryOverviewPanel({
  overview,
  audits,
  selectedAuditMemoryId,
  onAudit,
  onEdit,
  onDelete,
}: {
  overview: MemoryOverview;
  audits: MemoryAudit[];
  selectedAuditMemoryId: number | null;
  onAudit: (memoryId: number) => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memoryId: number) => void;
}) {
  const groups: Array<[string, Memory[]]> = [["当前会话", overview.session], ["当前项目", overview.project], ["用户记忆", overview.user]];
  return <section className="memory-overview panel"><div className="section-heading"><div><h2>记忆分层与审计</h2><p>会话记忆 → 项目记忆 → 用户记忆；删除操作只做软删除并保留审计记录。</p></div></div><div className="memory-scope-grid">{groups.map(([label, items]) => <article key={label}><h3>{label}<span>{items.length}</span></h3>{items.length === 0 ? <p className="muted">暂无</p> : <ul className="memory-list">{items.map(memory => <li key={memory.id}><span><small>{memory.memory_type} · 重要性 {memory.importance}</small>{memory.content}</span><div className="memory-actions"><button type="button" className="secondary-button" onClick={() => onAudit(memory.id)}>审计</button><button type="button" className="secondary-button" onClick={() => onEdit(memory)}>编辑</button><button type="button" className="delete-button" onClick={() => onDelete(memory.id)}>删除</button></div></li>)}</ul>}</article>)}</div>{selectedAuditMemoryId !== null && <div className="memory-audit"><h3>记忆 #{selectedAuditMemoryId} 的变更记录</h3>{audits.length === 0 ? <p className="muted">暂无审计记录</p> : <ul>{audits.map(audit => <li key={audit.id}><strong>{audit.action}</strong><span>{audit.reason}</span><small>{audit.created_at ?? ""}</small>{audit.after_content && <pre>{audit.after_content}</pre>}</li>)}</ul>}</div>}</section>;
}

function App() {
  const [prompt, setPrompt] = useState("");
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
  const [activeRunId, setActiveRunId] = useState<number | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectName, setProjectName] = useState("");
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<number | null>(null);
  const [sessionTitle, setSessionTitle] = useState("");
  const [folderName, setFolderName] = useState("");
  const [folderByProject, setFolderByProject] = useState<Record<number, string>>({});
  const [folderBound, setFolderBound] = useState(false);
  const [folderWritable, setFolderWritable] = useState(false);
  const [folderError, setFolderError] = useState("");
  const [folderSyncMessage, setFolderSyncMessage] = useState("");
  const [uploading, setUploading] = useState(false);
  const folderHandlesRef = useRef<Map<number, FileSystemDirectoryHandle>>(new Map());
  const localWriteQueueRef = useRef<Promise<void>>(Promise.resolve());
  const [approval, setApproval] = useState<Approval | null>(null);
  const [approvalAssistantId, setApprovalAssistantId] = useState<string | null>(null);
  const [approvalError, setApprovalError] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [planTasks, setPlanTasks] = useState<PlanTask[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryOverview, setMemoryOverview] = useState<MemoryOverview>({ session: [], project: [], user: [] });
  const [memoryAudits, setMemoryAudits] = useState<MemoryAudit[]>([]);
  const [selectedAuditMemoryId, setSelectedAuditMemoryId] = useState<number | null>(null);
  const [memoryContent, setMemoryContent] = useState("");
  const [memoryScope, setMemoryScope] = useState<"project" | "session" | "user">("project");
  const [memoryType, setMemoryType] = useState("fact");
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpBusy, setMcpBusy] = useState(false);
  const [mcpError, setMcpError] = useState("");
  const [expandedMcpServer, setExpandedMcpServer] = useState<string | null>(null);
  const [mcpToolCache, setMcpToolCache] = useState<Record<string, McpTool[]>>({});
  const [subagentCatalog, setSubagentCatalog] = useState<SubAgentCatalog | null>(null);

  useEffect(() => { fetch(`${API}/api/projects`).then(r => r.ok ? r.json() : []).then(setProjects).catch(() => undefined); }, []);
  useEffect(() => { void loadMcpServers(); }, []);
  useEffect(() => { fetch(`${API}/api/subagents`).then(r => r.ok ? r.json() : null).then(setSubagentCatalog).catch(() => undefined); }, []);
  useEffect(() => { if (plan) fetch(`${API}/api/plans/${plan.id}/tasks`).then(r => r.ok ? r.json() : []).then(setPlanTasks).catch(() => undefined); }, [plan?.id]);

  async function loadMcpServers() {
    try {
      const response = await fetch(`${API}/api/mcp/servers`);
      if (!response.ok) throw new Error(`MCP 服务读取失败 (${response.status})`);
      const payload = await response.json() as { servers?: McpServer[] };
      setMcpServers(payload.servers ?? []);
      setMcpError("");
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "MCP 服务读取失败");
    }
  }

  async function connectMcpServer(serverId: string) {
    if (mcpBusy) return;
    setMcpBusy(true); setMcpError("");
    try {
      const response = await fetch(`${API}/api/mcp/servers/${encodeURIComponent(serverId)}/connect`, { method: "POST" });
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `MCP 连接失败 (${response.status})`);
      await loadMcpServers();
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "MCP 连接失败");
    } finally { setMcpBusy(false); }
  }

  async function disconnectMcpServer(serverId: string) {
    if (mcpBusy) return;
    setMcpBusy(true); setMcpError("");
    try {
      const response = await fetch(`${API}/api/mcp/servers/${encodeURIComponent(serverId)}/disconnect`, { method: "POST" });
      if (!response.ok) throw new Error(`MCP 断开失败 (${response.status})`);
      await loadMcpServers();
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "MCP 断开失败");
    } finally { setMcpBusy(false); }
  }

  async function reconnectMcpServer(serverId: string) {
    if (mcpBusy) return;
    setMcpBusy(true); setMcpError("");
    try {
      const response = await fetch(`${API}/api/mcp/servers/${encodeURIComponent(serverId)}/reconnect`, { method: "POST" });
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `MCP 重连失败 (${response.status})`);
      setMcpToolCache(current => ({ ...current, [serverId]: [] }));
      await loadMcpServers();
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "MCP 重连失败");
    } finally { setMcpBusy(false); }
  }

  async function refreshMcpServers() {
    if (mcpBusy) return;
    setMcpBusy(true); setMcpError("");
    try {
      const response = await fetch(`${API}/api/mcp/refresh`, { method: "POST" });
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `MCP 刷新失败 (${response.status})`);
      await loadMcpServers();
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "MCP 刷新失败");
    } finally { setMcpBusy(false); }
  }

  async function toggleMcpTools(serverId: string) {
    if (expandedMcpServer === serverId) {
      setExpandedMcpServer(null);
      return;
    }
    setExpandedMcpServer(serverId);
    if (mcpToolCache[serverId]) return;
    try {
      const response = await fetch(`${API}/api/mcp/servers/${encodeURIComponent(serverId)}/tools`);
      if (!response.ok) throw new Error(`工具列表读取失败 (${response.status})`);
      const payload = await response.json() as { tools?: McpTool[] };
      setMcpToolCache(current => ({ ...current, [serverId]: payload.tools ?? [] }));
    } catch (error) {
      setMcpError(error instanceof Error ? error.message : "工具列表读取失败");
    }
  }

  async function loadMemories(projectId: number) {
    const r = await fetch(`${API}/api/projects/${projectId}/memories/overview`);
    if (!r.ok) return;
    const payload = await r.json() as { project?: Memory[]; global?: Memory[] };
    const nextOverview = { session: [], project: payload.project ?? [], user: payload.global ?? [] };
    setMemoryOverview(nextOverview);
    setMemories(nextOverview.project);
  }

  async function loadSessionMemoryOverview(sessionId: number) {
    const r = await fetch(`${API}/api/sessions/${sessionId}/memories/overview`);
    if (!r.ok) return;
    const payload = await r.json() as MemoryOverview;
    setMemoryOverview({ session: payload.session ?? [], project: payload.project ?? [], user: payload.user ?? [] });
    setMemories(payload.project ?? []);
  }

  async function loadMemoryAudit(memoryId: number) {
    const r = await fetch(`${API}/api/memories/${memoryId}/audit`);
    if (!r.ok) return;
    setSelectedAuditMemoryId(memoryId);
    setMemoryAudits(await r.json() as MemoryAudit[]);
  }

  async function selectProject(id: number) {
    setSelectedProject(id); setFolderName(folderByProject[id] ?? ""); setFolderBound(false); setFolderWritable(Boolean(folderHandlesRef.current.get(id))); setFolderError(""); setFolderSyncMessage(""); setChatMessages([]); setSelectedSession(null); setPlan(null); setPlanTasks([]); setRunStatus("idle"); await loadMemories(id);
    const workspaceResponse = await fetch(`${API}/api/projects/${id}/workspace`);
    if (workspaceResponse.ok) {
      const workspace = await workspaceResponse.json() as WorkspaceStatus;
      setFolderBound(workspace.bound);
      if (workspace.bound) {
        const savedFolderName = workspace.folder_name || folderByProject[id] || `项目工作区（${workspace.file_count} 个文件）`;
        setFolderName(savedFolderName);
        if (workspace.folder_name) setFolderByProject(current => ({ ...current, [id]: workspace.folder_name! }));
      }
    }
    const r = await fetch(`${API}/api/projects/${id}/sessions`);
    const list: ChatSession[] = r.ok ? await r.json() : [];
    setSessions(list);
    if (list.length) await selectSession(list[0].id);
  }

  async function selectSession(id: number) {
    setSelectedSession(id); setRunStatus("idle");
    void loadSessionMemoryOverview(id);
    const r = await fetch(`${API}/api/sessions/${id}/messages`);
    if (!r.ok) return;
    const messages = await r.json();
    setChatMessages(messages.map((message: { id: number; role: "user" | "assistant" | "system"; content: string }) => ({ id: `stored-${message.id}`, role: message.role, content: message.content, activities: [], status: "done" })));
  }

  async function createSession() {
    if (!selectedProject) return;
    const r = await fetch(`${API}/api/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: selectedProject, title: sessionTitle.trim() || "New session" }) });
    if (r.ok) { const session = await r.json(); setSessions(x => [session, ...x]); setSessionTitle(""); await selectSession(session.id); }
  }

  async function deleteSession(id: number) {
    if (!window.confirm("删除该会话及其消息？")) return;
    const r = await fetch(`${API}/api/sessions/${id}`, { method: "DELETE" });
    if (!r.ok) return;
    const remaining = sessions.filter(session => session.id !== id);
    setSessions(remaining);
    if (selectedSession === id) { if (remaining.length) await selectSession(remaining[0].id); else { setSelectedSession(null); setChatMessages([]); } }
  }

  async function createProject() {
    if (!projectName.trim()) return;
    const r = await fetch(`${API}/api/projects`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: projectName.trim() }) });
    if (r.ok) { const project = await r.json(); setProjects(x => [project, ...x]); setProjectName(""); await selectProject(project.id); }
  }

  async function deleteProject(id: number) {
    if (!window.confirm("删除项目及其已上传文件？")) return;
    const r = await fetch(`${API}/api/projects/${id}`, { method: "DELETE" });
    if (r.ok) { setProjects(x => x.filter(project => project.id !== id)); folderHandlesRef.current.delete(id); if (selectedProject === id) { setSelectedProject(null); setSelectedSession(null); setChatMessages([]); setMemories([]); setFolderBound(false); setFolderWritable(false); setFolderName(""); setFolderSyncMessage(""); } }
  }

  async function createMemory() {
    if (!selectedProject || !memoryContent.trim() || memoryBusy) return;
    setMemoryBusy(true);
    try {
      const r = await fetch(`${API}/api/memories`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: selectedProject, session_id: memoryScope === "session" ? selectedSession : null, scope_type: memoryScope, memory_type: memoryType, content: memoryContent.trim(), importance: 5, confidence: 0.9 }) });
      if (r.ok) { setMemoryContent(""); await reloadMemoryView(); }
    } finally { setMemoryBusy(false); }
  }

  async function reloadMemoryView() {
    if (selectedSession) {
      await loadSessionMemoryOverview(selectedSession);
    } else if (selectedProject) {
      await loadMemories(selectedProject);
    }
  }

  async function editMemory(memory: Memory) {
    const nextContent = window.prompt("修改长期记忆", memory.content)?.trim();
    if (!nextContent || nextContent === memory.content || memoryBusy) return;
    setMemoryBusy(true);
    try {
      const r = await fetch(`${API}/api/memories/${memory.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: nextContent }) });
      if (!r.ok) {
        const payload = await r.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail || `记忆修改失败 (${r.status})`);
      }
      await reloadMemoryView();
      await loadMemoryAudit(memory.id);
    } catch (error) {
      setFolderError(error instanceof Error ? error.message : "记忆修改失败");
    } finally { setMemoryBusy(false); }
  }

  async function removeMemory(id: number) {
    if (!window.confirm("删除这条长期记忆？删除后仍会保留审计记录。") || memoryBusy) return;
    setMemoryBusy(true);
    try {
      const r = await fetch(`${API}/api/memories/${id}`, { method: "DELETE" });
      if (!r.ok) throw new Error(`记忆删除失败 (${r.status})`);
      await reloadMemoryView();
    } catch (error) {
      setFolderError(error instanceof Error ? error.message : "记忆删除失败");
    } finally { setMemoryBusy(false); }
  }

  async function compactSelectedSession() {
    if (!selectedSession || memoryBusy) return;
    setMemoryBusy(true);
    try {
      const r = await fetch(`${API}/api/sessions/${selectedSession}/compact`, { method: "POST" });
      if (r.ok) {
        setChatMessages(current => [...current, { id: `context-${Date.now()}`, role: "system", content: "已生成会话摘要，后续请求将优先使用摘要和最近对话。", activities: [], status: "done" }]);
      }
    } finally { setMemoryBusy(false); }
  }

  const updateMessage = (id: string, updater: (message: ChatMessage) => ChatMessage) => setChatMessages(current => current.map(message => message.id === id ? updater(message) : message));

  function handleStreamEvent(item: StreamEvent, assistantId: string) {
    if (item.run_id) setActiveRunId(item.run_id);
    if (item.type === "message.delta") {
      updateMessage(assistantId, message => ({ ...message, content: message.content + (item.content ?? "") }));
      return;
    }
    if (item.type === "context.compacted") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: item.type, content: item.content, status: "info" }] }));
      return;
    }
    if (item.type === "mcp.server.starting" || item.type === "mcp.server.ready" || item.type === "mcp.server.stopped" || item.type === "mcp.tools.discovered" || item.type === "mcp.session.created" || item.type === "mcp.session.closed") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "context.compacted", content: `MCP：${item.type}${item.content ? ` · ${item.content}` : ""}`, status: "info" }] }));
      return;
    }
    if (item.type === "mcp.server.failed" || item.type === "mcp.registry.failed") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "context.compacted", content: `MCP：${item.content ?? "连接失败"}`, status: "error" }] }));
      return;
    }
    if (item.type === "mcp.tool.completed" || item.type === "mcp.tool.failed" || item.type === "mcp.tool.rejected") {
      const status = item.type === "mcp.tool.completed" ? "done" : "error";
      updateMessage(assistantId, message => {
        const activities = [...message.activities];
        const index = [...activities].reverse().findIndex(activity => activity.type === "tool" && activity.tool === item.tool && activity.status === "running");
        const target = index < 0 ? -1 : activities.length - 1 - index;
        if (target >= 0) activities[target] = { ...activities[target], content: item.content, status };
        return { ...message, activities };
      });
      return;
    }
    if (item.type === "agent.started") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "agent", tool: item.tool, arguments: item.content, status: "running" }] }));
      return;
    }
    if (item.type === "agent.finished" || item.type === "agent.failed") {
      const status = item.type === "agent.finished" ? "done" : "error";
      updateMessage(assistantId, message => {
        const activities = [...message.activities];
        const index = [...activities].reverse().findIndex(activity => activity.type === "agent" && activity.tool === item.tool && activity.status === "running");
        const target = index < 0 ? -1 : activities.length - 1 - index;
        if (target >= 0) activities[target] = { ...activities[target], content: item.content, status };
        return { ...message, activities };
      });
      return;
    }
    if (item.type === "tool.request") {
      let details: { calls?: ClientToolCall[] } = {};
      try { details = JSON.parse(item.content ?? "{}"); } catch { /* keep the stream usable if payload is malformed */ }
      if (item.run_id && Array.isArray(details.calls)) void handleClientToolRequests(item.run_id, details.calls, assistantId);
      return;
    }
    if (item.type === "file.write") {
      let details: { path?: string; content?: string } = {};
      try { details = JSON.parse(item.content ?? "{}"); } catch { /* keep the stream usable if payload is malformed */ }
      if (details.path && typeof details.content === "string") {
        void queueBoundFile(selectedProject, details.path, details.content);
      }
      return;
    }
    if (item.type === "tool.repeat_warning" || item.type === "tool.repeat_rejected" || item.type === "tool.circuit_broken") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "guard", tool: item.tool, content: item.content, status: "info" }] }));
      return;
    }
    if (item.type === "tool.blocked") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "tool", tool: item.tool, content: item.content, status: "error" }] }));
      return;
    }
    if (item.type === "tool.started") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "tool", tool: item.tool, arguments: item.content, status: "running" }] }));
      return;
    }
    if (item.type === "tool.finished") {
      updateMessage(assistantId, message => {
        const activities = [...message.activities];
        const index = [...activities].reverse().findIndex(activity => activity.type === "tool" && activity.tool === item.tool && activity.status === "running");
        const target = index < 0 ? -1 : activities.length - 1 - index;
        if (target >= 0) activities[target] = { ...activities[target], content: item.content, status: "done" };
        else {
          const existing = [...activities].reverse().findIndex(activity => activity.type === "tool" && activity.tool === item.tool);
          const existingIndex = existing < 0 ? -1 : activities.length - 1 - existing;
          if (existingIndex >= 0) activities[existingIndex] = { ...activities[existingIndex], content: item.content, status: "done" };
          else activities.push({ type: "tool", tool: item.tool, content: item.content, status: "done" });
        }
        return { ...message, activities };
      });
      return;
    }
    if (item.type === "approval.required") {
      let details: Record<string, unknown> = {};
      try { details = JSON.parse(item.content ?? "{}"); } catch { /* backend may already provide normalized approval fields */ }
      const approvalArguments = item.arguments ?? (details.arguments ? JSON.stringify(details.arguments, null, 2) : "{}");
      setApproval({
        ...item,
        approval_id: item.approval_id ?? (typeof details.approval_id === "number" ? details.approval_id : undefined),
        tool: item.tool ?? (typeof details.tool === "string" ? details.tool : undefined),
        risk_level: item.risk_level ?? (typeof details.risk_level === "string" ? details.risk_level : undefined),
        arguments: approvalArguments,
        target_path: item.target_path ?? (typeof details.target_path === "string" ? details.target_path : undefined),
        content: item.content && !item.approval_id && typeof details.reason === "string" ? details.reason : item.content,
      });
      setApprovalAssistantId(assistantId);
      setRunStatus("waiting");
      setApprovalError("");
      return;
    }
    if (item.type === "run.finished") {
      updateMessage(assistantId, message => ({ ...message, status: "done" }));
      setRunStatus(current => current === "waiting" ? current : "completed");
      setActiveRunId(null);
      return;
    }
    if (item.type === "run.limited") {
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: message.content || item.content || "本轮已达到最大执行轮次，任务尚未完成。" }));
      setRunStatus("limited");
      setActiveRunId(null);
      return;
    }
    if (item.type === "run.failed") {
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: message.content || item.content || "本次运行失败，请查看执行过程。" }));
      setRunStatus("failed");
      setActiveRunId(null);
      return;
    }
    if (item.type === "run.cancelled") {
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: message.content || item.content || "任务已取消。" }));
      setRunStatus("stopped");
      setActiveRunId(null);
      return;
    }
    if (item.type === "plan.created" && item.plan_id) { setPlan({ id: item.plan_id, summary: item.content, status: "WAITING_CONFIRMATION" }); return; }
    if (item.type === "plan.confirmation_required") { setPlan(current => current ? { ...current, goal: item.content } : current); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!prompt.trim() || running || approval || !selectedProject || !selectedSession) return;
    if (!folderBound || !folderWritable) {
      setFolderError("开始对话前，请选择可写的当前项目文件夹。Agent 修改代码后会同步回该文件夹。");
      return;
    }
    const value = prompt.trim();
    const assistantId = `assistant-${Date.now()}`;
    setPrompt(""); setRunning(true); setRunStatus("running");
    setChatMessages(current => [...current, { id: `user-${Date.now()}`, role: "user", content: value, activities: [], status: "done" }, { id: assistantId, role: "assistant", content: "", activities: [], status: "running" }]);
    try {
      const response = await fetch(`${API}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: value, project_id: selectedProject, session_id: selectedSession, local_workspace: true }) });
      if (!response.ok) throw new Error(`请求失败 (${response.status})`);
      const reader = response.body?.getReader();
      if (!reader) throw new Error("浏览器不支持流式响应");
      const decoder = new TextDecoder();
      let buffer = "";
      const consume = (chunk: string) => {
        for (const block of chunk.split("\n\n")) {
          const line = block.split("\n").find(value => value.startsWith("data: "));
          if (line) handleStreamEvent(JSON.parse(line.slice(6)) as StreamEvent, assistantId);
        }
      };
      while (true) {
        const result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        consume(chunks.join("\n\n"));
      }
      buffer += decoder.decode();
      if (buffer.trim()) consume(buffer);
    } catch (error) {
      setRunStatus("failed");
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: error instanceof Error ? error.message : "请求失败" }));
    } finally {
      await localWriteQueueRef.current;
      setRunning(false);
    }
  }

  async function cancelRun() {
    if (!activeRunId) return;
    try {
      const response = await fetch(`${API}/api/runs/${activeRunId}/cancel`, { method: "POST" });
      if (!response.ok) throw new Error(`取消任务失败 (${response.status})`);
      setRunStatus("stopped");
    } catch (error) {
      setFolderError(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  const skipDirs = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "coverage", ".idea", ".vscode", ".tox", ".mypy_cache", ".pytest_cache"]);
  const skipExtensions = new Set([".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".war", ".pdf", ".lock"]);
  const maxFileSize = 1024 * 1024;
  async function collectDirectory(handle: FileSystemDirectoryHandle, prefix = ""): Promise<DirectoryFile[]> { const result: DirectoryFile[] = []; for await (const entry of handle.values()) { if (entry.name.startsWith(".") && entry.name !== ".env.example") continue; const relative = prefix ? `${prefix}/${entry.name}` : entry.name; if (entry.kind === "file") { const ext = entry.name.includes(".") ? `.${entry.name.split(".").pop()!.toLowerCase()}` : ""; if (skipExtensions.has(ext)) continue; const file = await entry.getFile(); if (file.size > maxFileSize) continue; result.push({ file, relativePath: relative }); } else { if (skipDirs.has(entry.name)) continue; result.push(...await collectDirectory(entry, relative)); } } return result; }
  async function uploadBatch(items: DirectoryFile[], label: string, replace = false) {
    if (!selectedProject || !items.length) return;
    setFolderError(""); setUploading(true);
    try {
      for (let i = 0; i < items.length; i += 20) {
        const data = new FormData();
        items.slice(i, i + 20).forEach(item => { data.append("files", item.file, item.file.name); data.append("relative_paths", item.relativePath); });
        data.append("replace", replace && i === 0 ? "true" : "false");
        if (replace && i === 0) data.append("folder_name", label);
        const r = await fetch(`${API}/api/projects/${selectedProject}/files`, { method: "POST", body: data });
        if (!r.ok) throw new Error(`上传失败 (${r.status})`);
      }
      const displayName = label;
      setFolderName(displayName); setFolderBound(true);
      setFolderByProject(current => ({ ...current, [selectedProject]: displayName }));
    } catch (error) {
      setFolderError(error instanceof Error ? error.message : "文件夹绑定失败，请重试。");
    } finally { setUploading(false); }
  }
  async function chooseFolder() {
    if (!selectedProject || uploading) return;
    if (!window.showDirectoryPicker) {
      setFolderError("当前浏览器不支持可写目录绑定，请使用最新版 Chrome 或 Edge。");
      const input = document.createElement("input");
      input.type = "file"; input.multiple = true; input.setAttribute("webkitdirectory", ""); input.setAttribute("directory", "");
      input.onchange = () => { void uploadFolderFiles(Array.from(input.files ?? [])); };
      input.click();
      return;
    }
    if (folderBound && !window.confirm("切换文件夹会替换当前项目工作区中的文件，是否继续？")) return;
    try {
      const handle = await window.showDirectoryPicker({ mode: "readwrite" });
      folderHandlesRef.current.set(selectedProject, handle);
      setFolderWritable(true);
      await uploadBatch(await collectDirectory(handle), handle.name, true);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setFolderError(error instanceof Error ? error.message : "文件夹选择失败，请重试。");
    }
  }
  async function uploadFolderFiles(files: File[]) {
    if (!files.length || !selectedProject) return;
    if (folderBound && !window.confirm("切换文件夹会替换当前项目工作区中的文件，是否继续？")) return;
    setFolderWritable(false);
    const firstPath = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath ?? files[0].name;
    const label = firstPath.split("/")[0] || "所选文件夹";
    const items = files.filter(file => file.size <= maxFileSize && !skipExtensions.has(`.${file.name.split(".").pop()?.toLowerCase() ?? ""}`)).map(file => ({
      file,
      relativePath: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    }));
    await uploadBatch(items, label, true);
    setFolderError("当前浏览器只能上传文件夹，无法获得写回权限；请使用最新版 Chrome 或 Edge 进行代码同步。");
  }

  function localPathParts(relativePath: string): string[] {
    const normalized = relativePath.replaceAll("\\", "/").trim();
    if (!normalized || normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) throw new Error("Invalid local file path");
    const parts = normalized.split("/").filter(Boolean);
    if (!parts.length || parts.some(part => part === "." || part === "..")) throw new Error("Invalid local file path");
    return parts;
  }

  async function directoryAt(root: FileSystemDirectoryHandle, relativePath: string): Promise<FileSystemDirectoryHandle> {
    let directory = root;
    const parts = relativePath === "." || !relativePath ? [] : localPathParts(relativePath);
    for (const part of parts) directory = await directory.getDirectoryHandle(part);
    return directory;
  }

  async function fileAt(root: FileSystemDirectoryHandle, relativePath: string): Promise<{ file: File; path: string }> {
    const parts = localPathParts(relativePath);
    const directory = await directoryAt(root, parts.slice(0, -1).join("/") || ".");
    const handle = await directory.getFileHandle(parts[parts.length - 1]);
    return { file: await handle.getFile(), path: parts.join("/") };
  }

  async function listLocalDirectory(root: FileSystemDirectoryHandle, relativePath: string): Promise<string> {
    const directory = await directoryAt(root, relativePath || ".");
    const entries: string[] = [];
    for await (const entry of directory.values()) {
      if (entry.name.startsWith(".") && entry.name !== ".env.example") continue;
      entries.push(entry.kind === "directory" ? `${entry.name}/` : entry.name);
    }
    return entries.sort().join("\n") || "(empty)";
  }

  async function grepLocalDirectory(root: FileSystemDirectoryHandle, query: string, relativePath: string): Promise<string> {
    const matches: string[] = [];
    const walk = async (directory: FileSystemDirectoryHandle, prefix: string): Promise<void> => {
      if (matches.length >= 200) return;
      for await (const entry of directory.values()) {
        if (matches.length >= 200) return;
        if (entry.name.startsWith(".") && entry.name !== ".env.example") continue;
        if (entry.kind === "directory") {
          if (!skipDirs.has(entry.name)) await walk(entry, prefix ? `${prefix}/${entry.name}` : entry.name);
          continue;
        }
        const extension = entry.name.includes(".") ? `.${entry.name.split(".").pop()!.toLowerCase()}` : "";
        if (skipExtensions.has(extension)) continue;
        const file = await entry.getFile();
        if (file.size > maxFileSize) continue;
        const lines: string[] = (await file.text()).split(/\r?\n/);
        lines.forEach((line: string, index: number) => {
          if (matches.length < 200 && line.toLowerCase().includes(query.toLowerCase())) matches.push(`${prefix ? `${prefix}/` : ""}${entry.name}:${index + 1}:${line.trim()}`);
        });
      }
    };
    const startPath = relativePath && relativePath !== "." ? localPathParts(relativePath).join("/") : ".";
    await walk(await directoryAt(root, startPath), startPath === "." ? "" : startPath);
    return matches.join("\n") || "No matches";
  }

  async function writeBoundFileLegacy(projectId: number | null, relativePath: string, content: string) {
    if (!projectId) return { ok: false, error_type: "workspace_not_bound", result: "No local workspace is bound." };
    const handle = folderHandlesRef.current.get(projectId);
    if (!handle) {
      setFolderWritable(false);
      setFolderError("当前会话没有可写文件夹，请重新绑定。");
      return;
    }
    try {
      const parts = relativePath.replaceAll("\\", "/").split("/").filter(Boolean);
      if (!parts.length) return;
      if (parts.some(part => part === "." || part === "..")) throw new Error("文件路径无效");
      let directory = handle;
      for (const part of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(part, { create: true });
      const target = await directory.getFileHandle(parts[parts.length - 1], { create: true });
      const writable = await target.createWritable();
      try {
        await writable.write(content);
      } finally {
        await writable.close();
      }
      if (selectedProject === projectId) setFolderSyncMessage(`已将 ${relativePath} 写入本地绑定文件夹`);
    } catch (error) {
      setFolderWritable(false);
      setFolderError(error instanceof Error ? error.message : "本地文件写入失败，请重新绑定文件夹后重试。");
      return { ok: false, error_type: "local_write_failed", result: error instanceof Error ? error.message : "Local file write failed." };
    }
  }
  async function writeBoundFile(projectId: number | null, relativePath: string, content: string): Promise<ClientToolResult> {
    if (!projectId) return { ok: false, error_type: "workspace_not_bound", result: "No local workspace is bound." };
    const handle = folderHandlesRef.current.get(projectId);
    if (!handle) {
      setFolderWritable(false);
      setFolderError("No writable local workspace is bound.");
      return { ok: false, error_type: "workspace_not_bound", result: "No writable local workspace is bound." };
    }
    try {
      const parts = localPathParts(relativePath);
      let directory = handle;
      for (const part of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(part, { create: true });
      const target = await directory.getFileHandle(parts[parts.length - 1], { create: true });
      const writable = await target.createWritable();
      try {
        await writable.write(content);
      } finally {
        await writable.close();
      }
      if (selectedProject === projectId) setFolderSyncMessage(`Wrote ${relativePath} to the bound local workspace.`);
      return { ok: true, result: `Wrote ${relativePath}`, changed_paths: [parts.join("/")] };
    } catch (error) {
      setFolderWritable(false);
      const message = error instanceof Error ? error.message : "Local file write failed.";
      setFolderError(message);
      return { ok: false, error_type: "local_write_failed", result: message };
    }
  }

  function queueBoundFile(projectId: number | null, relativePath: string, content: string): Promise<ClientToolResult> {
    const result = localWriteQueueRef.current.then(() => writeBoundFile(projectId, relativePath, content));
    localWriteQueueRef.current = result.then(() => undefined);
    return result;
  }

  async function executeLocalCommand(call: ClientToolCall): Promise<ClientToolResult> {
    const args = call.arguments ?? {};
    if (typeof args.command !== "string" || !args.command.trim()) {
      return { ok: false, error_type: "invalid_arguments", result: "execute_command requires command." };
    }
    try {
      const response = await fetch(`${RUNNER_URL}/v1/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command: args.command,
          auto_prepare: args.auto_prepare !== false,
          timeout_seconds: typeof args.timeout_seconds === "number" ? args.timeout_seconds : 120,
        }),
      });
      const payload = await response.json().catch(() => ({})) as { ok?: boolean; result?: string; error_type?: string; dependency_steps?: Array<{ ecosystem?: string; manifest?: string; ok?: boolean; attempts?: Array<{ command?: string; output?: string }> }>; changed_paths?: string[] };
      if (!response.ok) return { ok: false, error_type: "runner_http_error", result: `Local Runner returned HTTP ${response.status}.` };
      const dependencyLog = (payload.dependency_steps ?? []).map(step => `${step.ok ? "prepared" : "failed"} ${step.ecosystem ?? "dependency"} (${step.manifest ?? "manifest"})`).join("\n");
      const result = [dependencyLog, payload.result ?? ""].filter(Boolean).join("\n\n");
      return {
        ok: payload.ok === true,
        // Keep the response within the backend's Pydantic request limit even
        // when the runner returned a full, capped command output plus logs.
        result: result.slice(0, 19_000),
        error_type: payload.error_type,
        changed_paths: payload.changed_paths ?? [],
      };
    } catch (error) {
      return {
        ok: false,
        error_type: "runner_unavailable",
        result: `Local Runner is unavailable at ${RUNNER_URL}. Start it with: python -m app.runner_server --workspace <your-folder>. (${error instanceof Error ? error.message : "connection failed"})`,
      };
    }
  }

  async function executeClientTool(projectId: number | null, call: ClientToolCall): Promise<ClientToolResult> {
    const handle = projectId ? folderHandlesRef.current.get(projectId) : undefined;
    if (!handle) return { ok: false, error_type: "workspace_not_bound", result: "No writable local workspace is bound." };
    const args = call.arguments ?? {};
    try {
      if (call.tool === "execute_command") return await executeLocalCommand(call);
      if (call.tool === "list_dir") {
        return { ok: true, result: await listLocalDirectory(handle, typeof args.path === "string" ? args.path : ".") };
      }
      if (call.tool === "read_file") {
        const target = await fileAt(handle, String(args.path ?? ""));
        const text = await target.file.text();
        return { ok: true, result: text.length > 12000 ? `${text.slice(0, 12000)}\n[truncated]` : text };
      }
      if (call.tool === "grep_code") {
        const query = typeof args.query === "string" ? args.query : "";
        if (!query) return { ok: false, error_type: "invalid_arguments", result: "grep_code requires query." };
        return { ok: true, result: await grepLocalDirectory(handle, query, typeof args.path === "string" ? args.path : ".") };
      }
      if (call.tool === "write_file") {
        if (typeof args.path !== "string" || typeof args.content !== "string") return { ok: false, error_type: "invalid_arguments", result: "write_file requires path and content." };
        return await queueBoundFile(projectId, args.path, args.content);
      }
      return { ok: false, error_type: "unsupported_client_tool", result: `Unsupported client tool: ${call.tool}` };
    } catch (error) {
      return { ok: false, error_type: "client_tool_failed", result: error instanceof Error ? error.message : "Client tool failed." };
    }
  }

  async function handleClientToolRequests(runId: number, calls: ClientToolCall[], assistantId: string) {
    for (const call of calls) {
      const result = await executeClientTool(selectedProject, call);
      try {
        const response = await fetch(`${API}/api/runs/${runId}/tool-results`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ invocation_id: call.invocation_id, ...result }),
        });
        if (!response.ok) throw new Error(`Tool result rejected (${response.status})`);
      } catch (error) {
        updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: "tool", tool: call.tool, content: error instanceof Error ? error.message : "Tool result submission failed.", status: "error" }] }));
      }
    }
  }

  async function uploadFiles(event: React.ChangeEvent<HTMLInputElement>) {
    if (!selectedProject || !event.target.files?.length) return;
    const items = Array.from(event.target.files).map(file => ({ file, relativePath: file.name }));
    await uploadBatch(items, `${event.target.files.length} 个文件`, false);
    event.target.value = "";
  }
  async function planAction(action: "confirm" | "cancel" | "pause" | "resume") { if (!plan) return; const r = await fetch(`${API}/api/plans/${plan.id}/${action}`, { method: "POST" }); if (r.ok) setPlan(await r.json()); }
  async function decideApproval(action: "approve" | "reject" | "cancel") {
    if (!approval?.approval_id) { setApprovalError("审批记录缺少 ID，请刷新页面后重新发起任务。"); return; }
    if (approvalBusy) return;
    setApprovalBusy(true); setRunning(true); setRunStatus("running");
    try {
      const response = await fetch(`${API}/api/approvals/${approval.approval_id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
      if (!response.ok) throw new Error(`审批请求失败（${response.status}）`);
      const result = await response.json() as { execution_result?: string | null; continuation_events?: StreamEvent[] };
      const activeApproval = approval;
      const activeAssistantId = approvalAssistantId;
      setApproval(null);
      setApprovalAssistantId(null);
      setApprovalError("");
      const actionLabel = action === "approve" ? "批准" : action === "reject" ? "拒绝" : "取消";
      const resultText = action === "approve" && result.execution_result ? `\n\n执行结果：\n${result.execution_result}` : "";
      const failed = action === "approve" && (result.execution_result ?? "").startsWith("审批后执行失败");
      const activityStatus: Activity["status"] = action === "approve" && !failed ? "done" : "error";
      const activityContent = action === "approve" ? (result.execution_result || "工具已执行。") : `审批已${actionLabel}，工具未执行。`;
      setChatMessages(current => {
        const updated = activeAssistantId
          ? current.map(message => {
              if (message.id !== activeAssistantId) return message;
              const activities = [...message.activities];
              const index = [...activities].reverse().findIndex(activity => activity.type === "tool" && activity.tool === activeApproval?.tool && activity.status === "running");
              const target = index < 0 ? -1 : activities.length - 1 - index;
              if (target >= 0) activities[target] = { ...activities[target], content: activityContent, status: activityStatus };
              return { ...message, activities };
            })
          : current;
        return [...updated, { id: `approval-${Date.now()}`, role: "system", content: `已${actionLabel}本次工具调用审批。${resultText}`, activities: [], status: "done" }];
      });
      const continuationEvents = result.continuation_events ?? [];
      for (const event of continuationEvents) handleStreamEvent(event, activeAssistantId ?? "");
      if (continuationEvents.length === 0) setRunStatus(action === "approve" ? "completed" : "stopped");
      if (action !== "approve") setRunStatus("stopped");
    } catch (error) {
      setRunStatus("failed");
      setApprovalError(error instanceof Error ? error.message : "审批请求失败，请稍后重试。");
    } finally {
      await localWriteQueueRef.current;
      setApprovalBusy(false); setRunning(false);
    }
  }
  async function copyMessage(content: string) { await navigator.clipboard?.writeText(content); }

  const statusView = approval
    ? { kind: "waiting", label: "等待确认" }
    : runStatus === "running"
      ? { kind: "running", label: "进行中" }
      : runStatus === "completed"
        ? { kind: "completed", label: "已完成" }
        : runStatus === "limited"
          ? { kind: "limited", label: "达到执行上限" }
        : runStatus === "failed"
          ? { kind: "failed", label: "执行失败" }
          : runStatus === "stopped"
            ? { kind: "stopped", label: "已停止" }
            : { kind: "idle", label: "就绪" };

  return <main>
    <header><div><h1>Huai-Coder</h1><span>Project Workspace Agent</span></div><div className="header-badge">安全执行 · 可追踪上下文</div></header>
    <section className="hero panel">
      <div className="hero-copy">
        <span className="eyebrow">PROJECT WORKSPACE AGENT</span>
        <h2>让 Agent 读懂项目，记住关键决策，稳妥完成任务</h2>
        <p>上传代码、确认计划、观察工具过程。Huai-Coder 将长期记忆、上下文压缩和人工审批整合到同一条项目工作流中。</p>
      </div>
      <div className="feature-grid">
        <div className="feature-card"><strong>长期记忆</strong><span>保存项目事实、决策和约束</span></div>
        <div className="feature-card"><strong>上下文压缩</strong><span>保留摘要与最近对话，控制 Token</span></div>
        <div className="feature-card"><strong>计划执行</strong><span>确认后按依赖逐步执行任务</span></div>
        <div className="feature-card"><strong>工具可追踪</strong><span>查看每次调用的参数和结果</span></div>
      </div>
      <div className="hero-art" aria-hidden="true"><span className="orbit orbit-one" /><span className="orbit orbit-two" /><span className="hero-mascot">麻</span><span className="hero-star star-one">✦</span><span className="hero-star star-two">✧</span></div>
    </section>
    <section className="projects panel"><h2>项目与会话</h2><div className="project-create"><input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="新建项目" /><button onClick={createProject}>创建</button></div><ul className="project-list">{projects.map(project => <li key={project.id} className={selectedProject === project.id ? "selected" : ""} onClick={() => selectProject(project.id)}><span>{project.name}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteProject(project.id); }}>删除</button></li>)}</ul>{selectedProject && <div className="project-tools"><div className="session-create"><input value={sessionTitle} onChange={event => setSessionTitle(event.target.value)} placeholder="新会话名称" /><button onClick={createSession}>新建会话</button></div><ul className="session-list">{sessions.map(session => <li key={session.id} className={selectedSession === session.id ? "selected" : ""} onClick={() => selectSession(session.id)}><span>{session.title}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteSession(session.id); }}>删除</button></li>)}</ul><div className="folder-area"><button type="button" className="secondary-button" onClick={chooseFolder} disabled={uploading}>{uploading ? "上传中…" : "绑定/切换文件夹"}</button><label className="secondary-button">选择文件<input type="file" multiple onChange={uploadFiles} disabled={uploading} /></label>{folderName && <span className="folder-name">当前绑定：{folderName}</span>}{!folderBound && <span className="folder-reminder">请先绑定对应文件夹</span>}{folderBound && !folderWritable && <span className="folder-reminder">请重新选择可写文件夹以同步代码</span>}{folderSyncMessage && <span className="folder-sync">{folderSyncMessage}</span>}{folderError && <span className="folder-error">{folderError}</span>}</div><div className="memory-panel"><div className="section-heading"><div><h3>长期记忆</h3><p>只保存可复用的项目事实、决策和约束</p></div><button className="secondary-button" type="button" onClick={compactSelectedSession} disabled={!selectedSession || memoryBusy}>压缩会话</button></div><div className="memory-create"><select value={memoryScope} onChange={event => setMemoryScope(event.target.value as "project" | "session" | "user")}><option value="project">项目记忆</option><option value="session" disabled={!selectedSession}>会话记忆</option><option value="user">用户记忆</option></select><select value={memoryType} onChange={event => setMemoryType(event.target.value)}><option value="fact">事实</option><option value="decision">决策</option><option value="preference">偏好</option><option value="constraint">约束</option><option value="task">待办</option></select><input value={memoryContent} onChange={event => setMemoryContent(event.target.value)} placeholder={`保存一条${memoryScope === "user" ? "用户" : memoryScope === "session" ? "会话" : "项目"}记忆`} /><button type="button" onClick={createMemory} disabled={memoryBusy || !memoryContent.trim() || (memoryScope === "session" && !selectedSession)}>保存</button></div><ul className="memory-list">{memories.length === 0 ? <li className="muted">暂无项目记忆</li> : memories.map(memory => <li key={memory.id}><span><small>{memory.memory_type} · 重要性 {memory.importance}</small>{memory.content}</span><button type="button" className="delete-button" onClick={() => removeMemory(memory.id)}>删除</button></li>)}</ul></div></div>}</section>
    {plan && <section className="plan-panel panel"><div className="section-heading"><div><h2>执行计划</h2><p>{plan.goal}</p></div><span className="status-pill">{plan.status}</span></div><p>{plan.summary}</p><div className="plan-tasks">{planTasks.length === 0 ? <p className="muted">正在加载任务列表…</p> : planTasks.map((task, index) => <article key={task.id}><strong>{index + 1}. {task.title}</strong><small>{task.task_key} · {task.task_type} · {task.status} · 重试 {task.retry_count}/2</small><div>{task.description}</div>{task.output_data && <pre>{task.output_data}</pre>}{task.error_message && <pre>{task.error_message}</pre>}</article>)}</div><div className="action-row"><button onClick={() => planAction("confirm")} disabled={plan.status !== "WAITING_CONFIRMATION"}>确认计划</button><button onClick={() => planAction("cancel")} disabled={plan.status === "SUCCEEDED" || plan.status === "CANCELLED"}>取消计划</button><button onClick={() => planAction("pause")} disabled={plan.status !== "RUNNING"}>暂停</button><button onClick={() => planAction("resume")} disabled={plan.status !== "PAUSED"}>继续</button></div></section>}
    <section className="chat panel"><div className="chat-heading"><div><h2>对话</h2><p>{folderBound && folderWritable ? `当前会话工作区：${folderName || "已绑定"}` : folderBound ? "请重新选择可写文件夹，代码才能同步回本地。" : "每次对话前，请先绑定当前项目对应的文件夹。"}</p></div><button type="button" className={`run-status ${statusView.kind}`} aria-live="polite" onClick={() => void cancelRun()} disabled={!running || !activeRunId} title={running ? "点击取消当前任务" : undefined}><span className="run-status-icon" aria-hidden="true" />{statusView.label}</button></div><div className="timeline">{chatMessages.length === 0 && <p className="empty">选择项目和会话后，开始向 Agent 提问。</p>}{chatMessages.map(message => <article className={`chat-message ${message.role} ${message.status ?? ""}`} key={message.id}><div className="message-head"><span className="role-label">{message.role === "user" ? "你" : message.role === "assistant" ? "Huai-Coder" : "系统"}</span>{message.role === "assistant" && message.content && <button className="copy-button" onClick={() => copyMessage(message.content)}>复制</button>}</div><ActivityList activities={message.activities} />{message.content ? <div className="message-content"><Markdown value={message.content} /></div> : message.status === "running" && <div className="typing"><i /><i /><i />正在思考…</div>}{message.status === "failed" && <div className="message-error">本次运行未完成，可以继续发送指令。</div>}</article>)}</div><form onSubmit={submit}><input value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={folderBound && folderWritable ? "描述你要完成的任务…" : "请先绑定可写文件夹，再开始对话"} disabled={running || !selectedSession || !folderBound || !folderWritable} /><button disabled={running || !selectedSession || !folderBound || !folderWritable}>{running ? "处理中…" : "发送"}</button></form></section>
    {approval && <div className="approval-modal"><div className="approval-card"><h2>需要审批：{approval.tool}</h2><p>风险等级：{approval.risk_level ?? "未标注"}</p><p>{approval.content}</p><pre>{approval.arguments}</pre><p>目标：{approval.target_path || "当前项目工作区"}</p>{approvalError && <p className="approval-error">{approvalError}</p>}<div className="action-row"><button onClick={() => decideApproval("approve")} disabled={approvalBusy}>{approvalBusy ? "处理中…" : "批准"}</button><button onClick={() => decideApproval("reject")} disabled={approvalBusy}>拒绝</button><button onClick={() => decideApproval("cancel")} disabled={approvalBusy}>取消</button></div></div></div>}
    <MemoryOverviewPanel overview={memoryOverview} audits={memoryAudits} selectedAuditMemoryId={selectedAuditMemoryId} onAudit={id => void loadMemoryAudit(id)} onEdit={memory => void editMemory(memory)} onDelete={id => void removeMemory(id)} />
    <McpDock servers={mcpServers} busy={mcpBusy} error={mcpError} expandedServer={expandedMcpServer} toolCache={mcpToolCache} onRefresh={() => void refreshMcpServers()} onConnect={id => void connectMcpServer(id)} onDisconnect={id => void disconnectMcpServer(id)} onReconnect={id => void reconnectMcpServer(id)} onToggleTools={id => void toggleMcpTools(id)} />
    <SubAgentDock catalog={subagentCatalog} />
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
