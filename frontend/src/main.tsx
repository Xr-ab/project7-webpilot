import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// React 18 的挂载方式：createRoot 而不是 ReactDOM.render
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
