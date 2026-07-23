import { ArrowLeft, ExternalLink } from "lucide-react";
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
    <header>
      <Brand />
      <button className="osk-invite-back" onClick={onBack}><ArrowLeft size={16} /> Voltar</button>
    </header>
    <main className="osk-invite-card">
      {server && <SmartAvatar className="osk-invite-server" src={server.icon} name={server.name} type="server" alt={server.name} size={72} />}
      <h1>Instale a Osaka neste servidor</h1>
      <h2 className="osk-invite-server-name" title={server?.name || "Servidor"}>{server?.name || "Servidor"}</h2>
      <p>A Osaka ainda não está neste servidor. Abra o convite no Discord e revise as permissões antes de instalar.</p>
      <button className="osk-primary-button osk-invite-action" onClick={onOpenInvite} disabled={busy}>
        {busy ? "Preparando convite..." : "Adicionar ao servidor"}
        <ExternalLink size={16} />
      </button>
    </main>
  </div>;
}
