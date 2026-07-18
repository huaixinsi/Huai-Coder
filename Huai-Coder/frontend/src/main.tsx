import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

function App() {
  return (
    <main>
      <header>
        <h1>Huai-Coder</h1>
        <span>Agent Workspace</span>
      </header>
      <section>
        <h2>Project Workspace</h2>
        <p>Phase 01 foundation services are ready.</p>
        <a href="http://localhost:8000/health">Check API health</a>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
