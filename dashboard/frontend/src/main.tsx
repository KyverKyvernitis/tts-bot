import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./design-refresh.css";
import "./yin-yang-theme.css";
import "./dashboard-design.css";
import "./module-artwork.css";
import "./typography.css";
import "./module-settings.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <div className="osk-root"><App /></div>
  </React.StrictMode>,
);
