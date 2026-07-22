import { ArrowLeft, Bot, ExternalLink, ShieldCheck } from "lucide-react";
import type { DashboardServerCard } from "../types/dashboard";
import { Brand } from "./BrowserLanding";
import { SmartAvatar } from "./SmartAvatar";

interface InviteScreenProps {
  server: DashboardServerCard | null;
  busy: boolean;
  onBack(): void;
  onOpenInvite(): void;
}

export function InviteScreen({ server, busy, onBack, onOpenInvite }: InviteScreenProps) {
  return <div className="osk-invite-shell">
    <header><Brand /><button className="osk-secondary-button osk-secondary-button--small" onClick={onBack}><ArrowLeft size={15} /> Voltar</button></header>
    <main className="osk-invite-card">
      <span className="osk-invite-bot"><Bot size={28} /></span>
      <span className="osk-kicker">Instalação necessária</span>
      {server && <SmartAvatar className="osk-invite-server" src={server.icon} name={server.name} type="server" alt={server.name} size={76} />}
      <h1>{server?.name || "Servidor"}</h1>
      <p>Adicione a Osaka neste servidor para liberar o painel. O Discord mostrará as permissões solicitadas antes da confirmação.</p>
      <div className="osk-invite-note"><ShieldCheck size={18} /><span><strong>Você mantém o controle</strong><small>O painel continuará validando sua permissão de gerenciamento em cada acesso.</small></span></div>
      <button className="osk-primary-button" onClick={onOpenInvite} disabled={busy}>{busy ? "Preparando convite..." : "Abrir convite no Discord"}<ExternalLink size={16} /></button>
    </main>
  </div>;
}
