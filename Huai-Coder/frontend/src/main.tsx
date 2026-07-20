import React, { FormEvent, StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type EventItem = { type: string; content?: string; tool?: string; approval_id?: number; risk_level?: string; arguments?: string; target_path?: string };
type Project = { id: number; name: string; description?: string };
type ChatSession = { id: number; project_id: number; title: string };
const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function App() {
  const [prompt, setPrompt] = useState("");
  const [events, setEvents] = useState<EventItem[]>([]);
  const [running, setRunning] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectName, setProjectName] = useState("");
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<number | null>(null);
  const [sessionTitle, setSessionTitle] = useState("");
  const [folderName, setFolderName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [approval, setApproval] = useState<EventItem | null>(null);

  useEffect(() => { fetch(`${API}/api/projects`).then(r => r.ok ? r.json() : []).then(setProjects).catch(() => undefined); }, []);
  async function selectProject(id: number) {
    setSelectedProject(id); setFolderName(""); setEvents([]); setSelectedSession(null);
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
    setEvents(messages.map((m: { role: string; content: string }) => ({ type: m.role, content: m.content })));
  }
  async function createSession() {
    if (!selectedProject) return;
    const r = await fetch(`${API}/api/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: selectedProject, title: sessionTitle.trim() || "New session" }) });
    if (r.ok) { const s = await r.json(); setSessions(x => [s, ...x]); setSessionTitle(""); await selectSession(s.id); }
  }
  async function deleteSession(id: number) {
    if (!window.confirm("删除该会话及其消息？")) return;
    const r = await fetch(`${API}/api/sessions/${id}`, { method: "DELETE" });
    if (!r.ok) return;
    const remaining = sessions.filter(s => s.id !== id);
    setSessions(remaining);
    if (selectedSession === id) {
      if (remaining.length) await selectSession(remaining[0].id);
      else { setSelectedSession(null); setEvents([]); }
    }
  }
  async function createProject() { if (!projectName.trim()) return; const r = await fetch(`${API}/api/projects`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: projectName.trim() }) }); if (r.ok) { const p = await r.json(); setProjects(x => [p, ...x]); setProjectName(""); await selectProject(p.id); } }
  async function deleteProject(id: number) { if (!window.confirm("删除项目及其已上传文件？")) return; const r = await fetch(`${API}/api/projects/${id}`, { method: "DELETE" }); if (r.ok) { setProjects(x => x.filter(p => p.id !== id)); if (selectedProject === id) { setSelectedProject(null); setFolderName(""); } } }
  async function uploadFolder(event: React.ChangeEvent<HTMLInputElement>) { if (!selectedProject || !event.target.files?.length) return; const first = event.target.files[0].webkitRelativePath || event.target.files[0].name; setFolderName(first.split("/")[0]); setUploading(true); const data = new FormData(); Array.from(event.target.files).forEach(file => { data.append("files", file); data.append("relative_paths", file.webkitRelativePath || file.name); }); try { const r = await fetch(`${API}/api/projects/${selectedProject}/files`, { method: "POST", body: data }); if (!r.ok) throw new Error("Upload failed"); } finally { setUploading(false); event.target.value = ""; } }
  async function submit(event: FormEvent) { event.preventDefault(); if (!prompt.trim() || running || !selectedProject || !selectedSession) return; const value = prompt.trim(); setPrompt(""); setRunning(true); setEvents(x => [...x, { type: "user", content: value }]); try { const r = await fetch(`${API}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: value, project_id: selectedProject, session_id: selectedSession }) }); if (!r.ok) throw new Error(`Request failed (${r.status})`); const reader = r.body?.getReader(); if (!reader) throw new Error("Streaming unavailable"); const decoder = new TextDecoder(); let buffer = ""; while (true) { const result = await reader.read(); if (result.done) break; buffer += decoder.decode(result.value, { stream: true }); const chunks = buffer.split("\n\n"); buffer = chunks.pop() ?? ""; for (const chunk of chunks) { const line = chunk.split("\n").find(x => x.startsWith("data: ")); if (line) { const item = JSON.parse(line.slice(6)); setEvents(x => [...x, item]); if (item.type === "approval.required") setApproval(item); } } } } catch (error) { setEvents(x => [...x, { type: "run.failed", content: error instanceof Error ? error.message : "Request failed" }]); } finally { setRunning(false); } }
  async function decideApproval(action: "approve" | "reject" | "cancel") { if (!approval?.approval_id) return; await fetch(`${API}/api/approvals/${approval.approval_id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) }); setApproval(null); }
  return <main><header><h1>Huai-Coder</h1><span>ReAct Agent</span></header><section className="projects"><h2>Projects</h2><div className="project-create"><input value={projectName} onChange={e => setProjectName(e.target.value)} placeholder="New project" /><button onClick={createProject}>Create</button></div><ul>{projects.map(p => <li key={p.id} className={selectedProject === p.id ? "selected" : ""} onClick={() => selectProject(p.id)}><span>{p.name}</span><button className="delete-button" onClick={e => { e.stopPropagation(); deleteProject(p.id); }}>删除</button></li>)}</ul>{selectedProject && <><div className="session-create"><input value={sessionTitle} onChange={e => setSessionTitle(e.target.value)} placeholder="Session name" /><button onClick={createSession}>New session</button></div><ul>{sessions.map(s => <li key={s.id} className={selectedSession === s.id ? "selected" : ""} onClick={() => selectSession(s.id)}><span>{s.title}</span><button className="delete-button" onClick={e => { e.stopPropagation(); deleteSession(s.id); }}>删除</button></li>)}</ul><div className="folder-area"><label className="folder-button">{uploading ? "Uploading..." : "选择文件夹"}<input type="file" /* @ts-expect-error */ webkitdirectory="" directory="" multiple onChange={uploadFolder} disabled={uploading} /></label>{folderName && <span className="folder-name">当前文件夹：{folderName}</span>}</div></>}</section><section className="chat"><div className="timeline">{events.length === 0 && <p className="empty">选择项目和会话后，再向 Agent 提问。</p>}{events.map((item, i) => <article className={item.type === "user" ? "user" : "agent"} key={i}><small>{item.tool ? `Tool: ${item.tool}` : item.type}</small><div>{item.content}</div></article>)}</div><form onSubmit={submit}><input value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Ask the agent..." disabled={running || !selectedSession} /><button disabled={running || !selectedSession}>{running ? "Running..." : "Send"}</button></form></section>{approval && <div className="approval-modal"><div className="approval-card"><h2>需要审批：{approval.tool}</h2><p>风险等级：{approval.risk_level}</p><p>{approval.content}</p><pre>{approval.arguments}</pre><p>目标：{approval.target_path}</p><button onClick={() => decideApproval("approve")}>批准</button><button onClick={() => decideApproval("reject")}>拒绝</button><button onClick={() => decideApproval("cancel")}>取消</button></div></div>}</main>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
