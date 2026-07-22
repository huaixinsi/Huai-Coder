import React, { FormEvent, StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./style.css";

type StreamEvent = {
  type: string;
  content?: string;
  tool?: string;
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
type Activity = { type: string; tool?: string; arguments?: string; content?: string; status: "running" | "done" | "info" | "error" };
type ChatMessage = { id: string; role: "user" | "assistant" | "system"; content: string; activities: Activity[]; status?: "running" | "done" | "failed" };
type Approval = StreamEvent;
type DirectoryFile = { file: File; relativePath: string };

declare global {
  interface Window { showDirectoryPicker?: (options?: { mode?: "read" | "readwrite" }) => Promise<FileSystemDirectoryHandle>; }
  interface FileSystemDirectoryHandle { values(): AsyncIterableIterator<FileSystemFileHandle | FileSystemDirectoryHandle>; }
  interface FileSystemFileHandle { kind: "file"; name: string; getFile(): Promise<File>; }
}

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function Markdown({ value }: { value: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{value}</ReactMarkdown>;
}

function ActivityList({ activities }: { activities: Activity[] }) {
  if (!activities.length) return null;
  const toolCalls = activities.filter(activity => activity.type === "tool");
  const finished = toolCalls.filter(activity => activity.status === "done").length;
  const formatArguments = (value?: string) => {
    if (!value) return "";
    try { return JSON.stringify(JSON.parse(value).arguments ?? JSON.parse(value), null, 2); } catch { return value; }
  };
  return <details className="execution-panel" open={activities.some(activity => activity.status === "running")}>
    <summary><span className="activity-dot" />执行过程 - {toolCalls.length} 次工具调用<span className="activity-status">已完成 {finished}/{toolCalls.length}</span></summary>
    <div className="activity-list">{activities.map((activity, index) => activity.type === "context.compacted" ? <div className="context-note" key={`context-${index}`}>上下文已压缩：{activity.content}</div> : <details className={`activity ${activity.status}`} key={`${activity.type}-${activity.tool ?? ""}-${index}`}>
      <summary><span className="activity-dot" />{index + 1}. {activity.tool ?? "工具调用"}<span className="activity-status">{activity.status === "running" ? "进行中" : "已完成"}</span></summary>
      <div className="activity-body">
        {activity.arguments && <><label>参数</label><pre>{formatArguments(activity.arguments)}</pre></>}
        {activity.content && <><label>结果</label><pre>{activity.content}</pre></>}
      </div>
    </details>)}</div>
  </details>;
}

function App() {
  const [prompt, setPrompt] = useState("");
  const [running, setRunning] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectName, setProjectName] = useState("");
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<number | null>(null);
  const [sessionTitle, setSessionTitle] = useState("");
  const [folderName, setFolderName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [planTasks, setPlanTasks] = useState<PlanTask[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryContent, setMemoryContent] = useState("");
  const [memoryType, setMemoryType] = useState("fact");
  const [memoryBusy, setMemoryBusy] = useState(false);

  useEffect(() => { fetch(`${API}/api/projects`).then(r => r.ok ? r.json() : []).then(setProjects).catch(() => undefined); }, []);
  useEffect(() => { if (plan) fetch(`${API}/api/plans/${plan.id}/tasks`).then(r => r.ok ? r.json() : []).then(setPlanTasks).catch(() => undefined); }, [plan?.id]);

  async function loadMemories(projectId: number) {
    const r = await fetch(`${API}/api/projects/${projectId}/memories`);
    if (r.ok) setMemories(await r.json());
  }

  async function selectProject(id: number) {
    setSelectedProject(id); setFolderName(""); setChatMessages([]); setSelectedSession(null); setPlan(null); setPlanTasks([]); await loadMemories(id);
    const r = await fetch(`${API}/api/projects/${id}/sessions`);
    const list: ChatSession[] = r.ok ? await r.json() : [];
    setSessions(list);
    if (list.length) await selectSession(list[0].id);
  }

  async function selectSession(id: number) {
    setSelectedSession(id);
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
    if (r.ok) { setProjects(x => x.filter(project => project.id !== id)); if (selectedProject === id) { setSelectedProject(null); setSelectedSession(null); setChatMessages([]); setMemories([]); } }
  }

  async function createMemory() {
    if (!selectedProject || !memoryContent.trim() || memoryBusy) return;
    setMemoryBusy(true);
    try {
      const r = await fetch(`${API}/api/memories`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: selectedProject, scope_type: "project", memory_type: memoryType, content: memoryContent.trim(), importance: 5, confidence: 0.9 }) });
      if (r.ok) { setMemoryContent(""); await loadMemories(selectedProject); }
    } finally { setMemoryBusy(false); }
  }

  async function removeMemory(id: number) {
    if (!selectedProject || !window.confirm("删除这条长期记忆？")) return;
    const r = await fetch(`${API}/api/memories/${id}`, { method: "DELETE" });
    if (r.ok) await loadMemories(selectedProject);
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
    if (item.type === "message.delta") {
      updateMessage(assistantId, message => ({ ...message, content: message.content + (item.content ?? "") }));
      return;
    }
    if (item.type === "context.compacted") {
      updateMessage(assistantId, message => ({ ...message, activities: [...message.activities, { type: item.type, content: item.content, status: "info" }] }));
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
        else activities.push({ type: "tool", tool: item.tool, content: item.content, status: "done" });
        return { ...message, activities };
      });
      return;
    }
    if (item.type === "approval.required") { setApproval(item); return; }
    if (item.type === "run.finished") { updateMessage(assistantId, message => ({ ...message, status: "done" })); return; }
    if (item.type === "run.failed") { updateMessage(assistantId, message => ({ ...message, status: "failed", content: message.content || item.content || "本次运行失败，请查看执行过程。" })); return; }
    if (item.type === "plan.created" && item.plan_id) { setPlan({ id: item.plan_id, summary: item.content, status: "WAITING_CONFIRMATION" }); return; }
    if (item.type === "plan.confirmation_required") { setPlan(current => current ? { ...current, goal: item.content } : current); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!prompt.trim() || running || !selectedProject || !selectedSession) return;
    const value = prompt.trim();
    const assistantId = `assistant-${Date.now()}`;
    setPrompt(""); setRunning(true);
    setChatMessages(current => [...current, { id: `user-${Date.now()}`, role: "user", content: value, activities: [], status: "done" }, { id: assistantId, role: "assistant", content: "", activities: [], status: "running" }]);
    try {
      const response = await fetch(`${API}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: value, project_id: selectedProject, session_id: selectedSession }) });
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
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: error instanceof Error ? error.message : "请求失败" }));
    } finally { setRunning(false); }
  }

  const skipDirs = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "coverage", ".idea", ".vscode", ".tox", ".mypy_cache", ".pytest_cache"]);
  const skipExtensions = new Set([".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".war", ".pdf", ".lock"]);
  const maxFileSize = 1024 * 1024;
  async function collectDirectory(handle: FileSystemDirectoryHandle, prefix = ""): Promise<DirectoryFile[]> { const result: DirectoryFile[] = []; for await (const entry of handle.values()) { if (entry.name.startsWith(".") && entry.name !== ".env.example") continue; const relative = prefix ? `${prefix}/${entry.name}` : entry.name; if (entry.kind === "file") { const ext = entry.name.includes(".") ? `.${entry.name.split(".").pop()!.toLowerCase()}` : ""; if (skipExtensions.has(ext)) continue; const file = await entry.getFile(); if (file.size > maxFileSize) continue; result.push({ file, relativePath: relative }); } else { if (skipDirs.has(entry.name)) continue; result.push(...await collectDirectory(entry, relative)); } } return result; }
  async function uploadBatch(items: DirectoryFile[], label: string) { if (!selectedProject || !items.length) return; setFolderName(`${label}（${items.length} 个文件）`); setUploading(true); try { for (let i = 0; i < items.length; i += 20) { const data = new FormData(); items.slice(i, i + 20).forEach(item => { data.append("files", item.file, item.file.name); data.append("relative_paths", item.relativePath); }); const r = await fetch(`${API}/api/projects/${selectedProject}/files`, { method: "POST", body: data }); if (!r.ok) throw new Error(`上传失败 (${r.status})`); } } finally { setUploading(false); } }
  async function chooseFolder() { if (!selectedProject || uploading || !window.showDirectoryPicker) return; try { const handle = await window.showDirectoryPicker({ mode: "read" }); await uploadBatch(await collectDirectory(handle), handle.name); } catch (error) { if (!(error instanceof DOMException && error.name === "AbortError")) console.error(error); } }
  async function uploadFiles(event: React.ChangeEvent<HTMLInputElement>) { if (!selectedProject || !event.target.files?.length) return; const data = new FormData(); Array.from(event.target.files).forEach(file => { data.append("files", file); data.append("relative_paths", file.name); }); setUploading(true); try { const r = await fetch(`${API}/api/projects/${selectedProject}/files`, { method: "POST", body: data }); if (r.ok) setFolderName(`${event.target.files.length} 个文件`); } finally { setUploading(false); event.target.value = ""; } }
  async function planAction(action: "confirm" | "cancel" | "pause" | "resume") { if (!plan) return; const r = await fetch(`${API}/api/plans/${plan.id}/${action}`, { method: "POST" }); if (r.ok) setPlan(await r.json()); }
  async function decideApproval(action: "approve" | "reject" | "cancel") { if (!approval?.approval_id) return; await fetch(`${API}/api/approvals/${approval.approval_id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) }); setApproval(null); }
  async function copyMessage(content: string) { await navigator.clipboard?.writeText(content); }

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
    </section>
    <section className="projects panel"><h2>项目与会话</h2><div className="project-create"><input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="新建项目" /><button onClick={createProject}>创建</button></div><ul className="project-list">{projects.map(project => <li key={project.id} className={selectedProject === project.id ? "selected" : ""} onClick={() => selectProject(project.id)}><span>{project.name}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteProject(project.id); }}>删除</button></li>)}</ul>{selectedProject && <div className="project-tools"><div className="session-create"><input value={sessionTitle} onChange={event => setSessionTitle(event.target.value)} placeholder="新会话名称" /><button onClick={createSession}>新建会话</button></div><ul className="session-list">{sessions.map(session => <li key={session.id} className={selectedSession === session.id ? "selected" : ""} onClick={() => selectSession(session.id)}><span>{session.title}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteSession(session.id); }}>删除</button></li>)}</ul><div className="folder-area"><button type="button" className="secondary-button" onClick={chooseFolder} disabled={uploading}>{uploading ? "上传中…" : "选择完整文件夹"}</button><label className="secondary-button">选择文件<input type="file" multiple onChange={uploadFiles} disabled={uploading} /></label>{folderName && <span className="folder-name">最近上传：{folderName}</span>}</div><div className="memory-panel"><div className="section-heading"><div><h3>长期记忆</h3><p>只保存可复用的项目事实、决策和约束</p></div><button className="secondary-button" type="button" onClick={compactSelectedSession} disabled={!selectedSession || memoryBusy}>压缩会话</button></div><div className="memory-create"><select value={memoryType} onChange={event => setMemoryType(event.target.value)}><option value="fact">事实</option><option value="decision">决策</option><option value="preference">偏好</option><option value="constraint">约束</option><option value="task">待办</option></select><input value={memoryContent} onChange={event => setMemoryContent(event.target.value)} placeholder="保存一条项目记忆" /><button type="button" onClick={createMemory} disabled={memoryBusy || !memoryContent.trim()}>保存</button></div><ul className="memory-list">{memories.length === 0 ? <li className="muted">暂无项目记忆</li> : memories.map(memory => <li key={memory.id}><span><small>{memory.memory_type} · 重要性 {memory.importance}</small>{memory.content}</span><button type="button" className="delete-button" onClick={() => removeMemory(memory.id)}>删除</button></li>)}</ul></div></div>}</section>
    {plan && <section className="plan-panel panel"><div className="section-heading"><div><h2>执行计划</h2><p>{plan.goal}</p></div><span className="status-pill">{plan.status}</span></div><p>{plan.summary}</p><div className="plan-tasks">{planTasks.length === 0 ? <p className="muted">正在加载任务列表…</p> : planTasks.map((task, index) => <article key={task.id}><strong>{index + 1}. {task.title}</strong><small>{task.task_key} · {task.task_type} · {task.status} · 重试 {task.retry_count}/2</small><div>{task.description}</div>{task.output_data && <pre>{task.output_data}</pre>}{task.error_message && <pre>{task.error_message}</pre>}</article>)}</div><div className="action-row"><button onClick={() => planAction("confirm")} disabled={plan.status !== "WAITING_CONFIRMATION"}>确认计划</button><button onClick={() => planAction("cancel")} disabled={plan.status === "SUCCEEDED" || plan.status === "CANCELLED"}>取消计划</button><button onClick={() => planAction("pause")} disabled={plan.status !== "RUNNING"}>暂停</button><button onClick={() => planAction("resume")} disabled={plan.status !== "PAUSED"}>继续</button></div></section>}
    <section className="chat panel"><div className="chat-heading"><div><h2>对话</h2><p>助手回复支持 Markdown；工具调用和上下文压缩显示在执行过程里</p></div>{running && <span className="running-pill"><i />处理中</span>}</div><div className="timeline">{chatMessages.length === 0 && <p className="empty">选择项目和会话后，开始向 Agent 提问。</p>}{chatMessages.map(message => <article className={`chat-message ${message.role} ${message.status ?? ""}`} key={message.id}><div className="message-head"><span className="role-label">{message.role === "user" ? "你" : message.role === "assistant" ? "Huai-Coder" : "系统"}</span>{message.role === "assistant" && message.content && <button className="copy-button" onClick={() => copyMessage(message.content)}>复制</button>}</div><ActivityList activities={message.activities} />{message.content ? <div className="message-content"><Markdown value={message.content} /></div> : message.status === "running" && <div className="typing"><i /><i /><i />正在思考…</div>}{message.status === "failed" && <div className="message-error">本次运行未完成，可以继续发送指令。</div>}</article>)}</div><form onSubmit={submit}><input value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="描述你要完成的任务…" disabled={running || !selectedSession} /><button disabled={running || !selectedSession}>{running ? "处理中…" : "发送"}</button></form></section>
    {approval && <div className="approval-modal"><div className="approval-card"><h2>需要审批：{approval.tool}</h2><p>风险等级：{approval.risk_level}</p><p>{approval.content}</p><pre>{approval.arguments}</pre><p>目标：{approval.target_path}</p><div className="action-row"><button onClick={() => decideApproval("approve")}>批准</button><button onClick={() => decideApproval("reject")}>拒绝</button><button onClick={() => decideApproval("cancel")}>取消</button></div></div></div>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
