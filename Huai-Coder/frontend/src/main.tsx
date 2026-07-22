import React, { FormEvent, StrictMode, useEffect, useRef, useState } from "react";
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
type WorkspaceStatus = { bound: boolean; file_count: number; folder_name?: string | null };
type RunStatus = "idle" | "running" | "waiting" | "completed" | "failed" | "stopped";

declare global {
  interface Window { showDirectoryPicker?: (options?: { mode?: "read" | "readwrite" }) => Promise<FileSystemDirectoryHandle>; }
  interface FileSystemDirectoryHandle { name: string; values(): AsyncIterableIterator<FileSystemFileHandle | FileSystemDirectoryHandle>; getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<FileSystemDirectoryHandle>; getFileHandle(name: string, options?: { create?: boolean }): Promise<FileSystemFileHandle>; queryPermission?(descriptor?: { mode?: "read" | "readwrite" }): Promise<PermissionState>; }
  interface FileSystemFileHandle { kind: "file"; name: string; getFile(): Promise<File>; createWritable(): Promise<FileSystemWritableFileStream>; }
  interface FileSystemWritableFileStream { write(data: string): Promise<void>; close(): Promise<void>; }
}

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function Markdown({ value }: { value: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{value}</ReactMarkdown>;
}

function ActivityList({ activities }: { activities: Activity[] }) {
  if (!activities.length) return null;
  const toolCalls = activities.filter(activity => activity.type === "tool");
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

function App() {
  const [prompt, setPrompt] = useState("");
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
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
        else activities.push({ type: "tool", tool: item.tool, content: item.content, status: "done" });
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
      return;
    }
    if (item.type === "run.failed") {
      updateMessage(assistantId, message => ({ ...message, status: "failed", content: message.content || item.content || "本次运行失败，请查看执行过程。" }));
      setRunStatus("failed");
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

  async function writeBoundFile(projectId: number | null, relativePath: string, content: string) {
    if (!projectId) return;
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
    }
  }
  function queueBoundFile(projectId: number | null, relativePath: string, content: string) {
    localWriteQueueRef.current = localWriteQueueRef.current.then(() => writeBoundFile(projectId, relativePath, content));
    return localWriteQueueRef.current;
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
    <section className="projects panel"><h2>项目与会话</h2><div className="project-create"><input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="新建项目" /><button onClick={createProject}>创建</button></div><ul className="project-list">{projects.map(project => <li key={project.id} className={selectedProject === project.id ? "selected" : ""} onClick={() => selectProject(project.id)}><span>{project.name}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteProject(project.id); }}>删除</button></li>)}</ul>{selectedProject && <div className="project-tools"><div className="session-create"><input value={sessionTitle} onChange={event => setSessionTitle(event.target.value)} placeholder="新会话名称" /><button onClick={createSession}>新建会话</button></div><ul className="session-list">{sessions.map(session => <li key={session.id} className={selectedSession === session.id ? "selected" : ""} onClick={() => selectSession(session.id)}><span>{session.title}</span><button className="delete-button" onClick={event => { event.stopPropagation(); deleteSession(session.id); }}>删除</button></li>)}</ul><div className="folder-area"><button type="button" className="secondary-button" onClick={chooseFolder} disabled={uploading}>{uploading ? "上传中…" : "绑定/切换文件夹"}</button><label className="secondary-button">选择文件<input type="file" multiple onChange={uploadFiles} disabled={uploading} /></label>{folderName && <span className="folder-name">当前绑定：{folderName}</span>}{!folderBound && <span className="folder-reminder">请先绑定对应文件夹</span>}{folderBound && !folderWritable && <span className="folder-reminder">请重新选择可写文件夹以同步代码</span>}{folderSyncMessage && <span className="folder-sync">{folderSyncMessage}</span>}{folderError && <span className="folder-error">{folderError}</span>}</div><div className="memory-panel"><div className="section-heading"><div><h3>长期记忆</h3><p>只保存可复用的项目事实、决策和约束</p></div><button className="secondary-button" type="button" onClick={compactSelectedSession} disabled={!selectedSession || memoryBusy}>压缩会话</button></div><div className="memory-create"><select value={memoryType} onChange={event => setMemoryType(event.target.value)}><option value="fact">事实</option><option value="decision">决策</option><option value="preference">偏好</option><option value="constraint">约束</option><option value="task">待办</option></select><input value={memoryContent} onChange={event => setMemoryContent(event.target.value)} placeholder="保存一条项目记忆" /><button type="button" onClick={createMemory} disabled={memoryBusy || !memoryContent.trim()}>保存</button></div><ul className="memory-list">{memories.length === 0 ? <li className="muted">暂无项目记忆</li> : memories.map(memory => <li key={memory.id}><span><small>{memory.memory_type} · 重要性 {memory.importance}</small>{memory.content}</span><button type="button" className="delete-button" onClick={() => removeMemory(memory.id)}>删除</button></li>)}</ul></div></div>}</section>
    {plan && <section className="plan-panel panel"><div className="section-heading"><div><h2>执行计划</h2><p>{plan.goal}</p></div><span className="status-pill">{plan.status}</span></div><p>{plan.summary}</p><div className="plan-tasks">{planTasks.length === 0 ? <p className="muted">正在加载任务列表…</p> : planTasks.map((task, index) => <article key={task.id}><strong>{index + 1}. {task.title}</strong><small>{task.task_key} · {task.task_type} · {task.status} · 重试 {task.retry_count}/2</small><div>{task.description}</div>{task.output_data && <pre>{task.output_data}</pre>}{task.error_message && <pre>{task.error_message}</pre>}</article>)}</div><div className="action-row"><button onClick={() => planAction("confirm")} disabled={plan.status !== "WAITING_CONFIRMATION"}>确认计划</button><button onClick={() => planAction("cancel")} disabled={plan.status === "SUCCEEDED" || plan.status === "CANCELLED"}>取消计划</button><button onClick={() => planAction("pause")} disabled={plan.status !== "RUNNING"}>暂停</button><button onClick={() => planAction("resume")} disabled={plan.status !== "PAUSED"}>继续</button></div></section>}
    <section className="chat panel"><div className="chat-heading"><div><h2>对话</h2><p>{folderBound && folderWritable ? `当前会话工作区：${folderName || "已绑定"}` : folderBound ? "请重新选择可写文件夹，代码才能同步回本地。" : "每次对话前，请先绑定当前项目对应的文件夹。"}</p></div><button type="button" className={`run-status ${statusView.kind}`} aria-live="polite" disabled><span className="run-status-icon" aria-hidden="true" />{statusView.label}</button></div><div className="timeline">{chatMessages.length === 0 && <p className="empty">选择项目和会话后，开始向 Agent 提问。</p>}{chatMessages.map(message => <article className={`chat-message ${message.role} ${message.status ?? ""}`} key={message.id}><div className="message-head"><span className="role-label">{message.role === "user" ? "你" : message.role === "assistant" ? "Huai-Coder" : "系统"}</span>{message.role === "assistant" && message.content && <button className="copy-button" onClick={() => copyMessage(message.content)}>复制</button>}</div><ActivityList activities={message.activities} />{message.content ? <div className="message-content"><Markdown value={message.content} /></div> : message.status === "running" && <div className="typing"><i /><i /><i />正在思考…</div>}{message.status === "failed" && <div className="message-error">本次运行未完成，可以继续发送指令。</div>}</article>)}</div><form onSubmit={submit}><input value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={folderBound && folderWritable ? "描述你要完成的任务…" : "请先绑定可写文件夹，再开始对话"} disabled={running || !selectedSession || !folderBound || !folderWritable} /><button disabled={running || !selectedSession || !folderBound || !folderWritable}>{running ? "处理中…" : "发送"}</button></form></section>
    {approval && <div className="approval-modal"><div className="approval-card"><h2>需要审批：{approval.tool}</h2><p>风险等级：{approval.risk_level ?? "未标注"}</p><p>{approval.content}</p><pre>{approval.arguments}</pre><p>目标：{approval.target_path || "当前项目工作区"}</p>{approvalError && <p className="approval-error">{approvalError}</p>}<div className="action-row"><button onClick={() => decideApproval("approve")} disabled={approvalBusy}>{approvalBusy ? "处理中…" : "批准"}</button><button onClick={() => decideApproval("reject")} disabled={approvalBusy}>拒绝</button><button onClick={() => decideApproval("cancel")} disabled={approvalBusy}>取消</button></div></div></div>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
