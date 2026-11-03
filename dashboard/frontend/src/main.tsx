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
import "./landing-refresh.css";
import "./account-menu-refresh.css";
import "./workspace-foundation.css";
import "./workspace-pages.css";
import "./workspace-modules.css";
import "./workspace-navigation.css";
import "./workspace-editors.css";
import "./workspace-servers.css";
import "./workspace-module-details.css";
import "./message-editor-refinement.css";
import "./discord-preview-refinement.css";
import "./message-media-refinement.css";
import "./ttv-settings.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <div className="osk-root"><App /></div>
  </React.StrictMode>,
);
