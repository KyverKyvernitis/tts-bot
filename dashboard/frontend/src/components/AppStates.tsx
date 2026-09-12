import { AlertTriangle, ArrowRight, LogIn, RefreshCw, X } from "lucide-react";
import { LoadingProgress, LoadingVisual } from "./VisualTemplates";

export function FullPageLoading({ progress }: { progress: number }) {
  return <div className="osk-full-loading" aria-busy="true"><LoadingVisual size={30} /><LoadingProgress progress={progress} label="Carregando o painel" /></div>;
}

export function LoginRequired({ onLogin, onHome }: { onLogin(): void; onHome(): void }) {
  return <div className="osk-login-required"><div><span><LogIn size={24} /></span><h1>Entre para continuar</h1><p>O painel precisa confirmar sua conta e as permissões do servidor pelo Discord.</p><button className="osk-primary-button" onClick={onLogin}>Entrar com Discord<ArrowRight size={16} /></button><button className="osk-secondary-button" onClick={onHome}>Voltar ao site</button></div></div>;
}

export function Notice({ type, text, onClose }: { type: "error" | "success" | "info"; text: string; onClose(): void }) {
  return <div className="osk-global-notice" data-type={type} role="status"><span>{type === "error" ? <AlertTriangle size={17} /> : type === "success" ? <RefreshCw size={17} /> : null}{text}</span><button onClick={onClose} aria-label="Fechar aviso"><X size={16} /></button></div>;
}
