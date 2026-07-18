import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

function App() {
  return <main><header><h1>Huai-Coder</h1><span>Agent 工作台</span></header><section><h2>项目工作区</h2><p>第一阶段基础设施已启动，后续将在此接入项目、会话和消息工作流。</p><a href="http://localhost:8000/health">检查 API 健康状态</a></section></main>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
