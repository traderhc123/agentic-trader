import React from "react";
import ReactDOM from "react-dom/client";
import * as Tooltip from "@radix-ui/react-tooltip";
import "./index.css";
import { Dashboard } from "./dash/Dashboard";
import { Wizard } from "./wizard/Wizard";

// One bundle serves both pages; the agent routes /setup and /dash here.
const path = location.pathname.replace(/\/+$/, "");
const isSetup = path === "/setup";
document.title = isSetup ? "Agentic Trader · Setup" : "Agentic Trader";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Tooltip.Provider delayDuration={300}>{isSetup ? <Wizard /> : <Dashboard />}</Tooltip.Provider>
  </React.StrictMode>,
);
