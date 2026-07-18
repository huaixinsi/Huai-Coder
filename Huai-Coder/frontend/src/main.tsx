import { FormEvent, StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type EventItem = { type: string; content?: string; tool?: string };
const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function App() {
  const [prompt, setPrompt] = useState("");
  const [events, setEvents] = useState<EventItem[]>([]);
  const [running, setRunning] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!prompt.trim() || running) return;
    const value = prompt.trim(); setPrompt(""); setRunning(true); setEvents(items => [...items, { type: "user", content: value }]);
    const response = await fetch(`${API}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: value }) });
    const reader = response.body?.getReader(); const decoder = new TextDecoder();
    if (reader) { let buffer = ""; while (true) { const result = await reader.read(); if (result.done) break; buffer += decoder.decode(result.value, { stream: true }); const chunks = buffer.split("\n\n"); buffer = chunks.pop() ?? ""; for (const chunk of chunks) { const line = chunk.split("\n").find(item => item.startsWith("data: ")); if (line) setEvents(items => [...items, JSON.parse(line.slice(6))]); } } }
    setRunning(false);
  }
  return <main><header><h1>Huai-Coder</h1><span>ReAct Agent</span></header><section className="chat"><div className="timeline">{events.length === 0 && <p className="empty">Ask the agent a question, or use /list .</p>}{events.map((item, index) => <article className={item.type === "user" ? "user" : "agent"} key={index}><small>{item.tool ? `Tool: ${item.tool}` : item.type}</small><div>{item.content}</div></article>)}</div><form onSubmit={submit}><input value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="Ask the agent..." disabled={running} /><button disabled={running}>{running ? "Running..." : "Send"}</button></form></section></main>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
