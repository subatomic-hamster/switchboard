import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ClaimPage } from "./ClaimPage";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
// A claim link is `/claim#<id>:<secret>`: a standalone page, none of the dashboard.
createRoot(root).render(
  <StrictMode>{window.location.pathname === "/claim" ? <ClaimPage /> : <App />}</StrictMode>,
);
