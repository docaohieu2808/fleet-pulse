import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import FleetPulse from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FleetPulse />
  </StrictMode>,
);
