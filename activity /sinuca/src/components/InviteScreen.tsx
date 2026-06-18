import { Bot } from "lucide-react";
import type { DashboardServerCard } from "../types/dashboard";

interface InviteScreenProps {
  server: DashboardServerCard | null;
  busy: boolean;
  onBack(): void;
  onOpenInvite(): void;
}

export function InviteScreen({ server, busy, onBack, onOpenInvite }: InviteScreenProps) {
  return (
    <div className="osk-browser">
      <section className="osk-invite">
        <div className="osk-invite-orb">
          <Bot size={32} />
        </div>
        <span className="osk-hero-eyebrow">Convidar bot</span>
        <h1>{server ? server.name : "Servidor"}</h1>
        <p>
          Para configurar este servidor pelo Dashboard, primeiro adicione o bot com as permissões
          necessárias. Vai abrir a janela oficial do Discord.
        </p>
        <div className="osk-invite-actions">
          <button className="osk-btn" onClick={onBack}>
            Voltar
          </button>
          <button
            className="osk-btn osk-btn--primary"
            onClick={onOpenInvite}
            disabled={busy || !server}
          >
            {busy ? "Preparando..." : "Convidar bot"}
          </button>
        </div>
      </section>
    </div>
  );
}
