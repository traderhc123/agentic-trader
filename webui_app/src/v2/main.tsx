import React from "react";
import ReactDOM from "react-dom/client";
import "./guardian.css";
import { App } from "./App";

// The /v2 dashboard: its own entry, its own styles. The classic dashboard
// (src/main.tsx) shares no code with it at run time.
try {
  const t = localStorage.getItem("at-theme");
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
} catch {
  /* private mode: follow the computer's setting */
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
