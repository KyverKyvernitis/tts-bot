import { useEffect, useState } from "react";
import { bootstrapDiscord } from "./sdk/discord";
import type { ActivityBootstrap } from "./types/activity";
import StatusCard from "./ui/StatusCard";

const initialState: ActivityBootstrap = {
  sdkReady: false,
  clientId: null,
  context: {
    mode: "casual",
    instanceId: null,
    guildId: null,
    channelId: null,
    source: "fallback",
  },
};

export default function App() {
  const [state, setState] = useState<ActivityBootstrap>(initialState);

  useEffect(() => {
    let mounted = true;
    bootstrapDiscord().then((next) => {
      if (mounted) setState(next);
    });
    return () => {
      mounted = false;
    };
  }, []);

  const { context } = state;
  const modeLabel = context.mode === "server" ? "Partida com fichas" : "Partida casual";
  const contextText = context.mode === "server"
    ? "Aberta em servidor. A economia entra só quando o lobby econômico estiver ativo."
    : "Aberta fora de servidor. Nesta fase a sinuca funciona sem fichas.";

  return (
    <main className="app-shell">
      <header className="hero-card">
        <div>
          <span className="hero-card__eyebrow">Sinuca Activity</span>
          <h1>Base da activity pronta</h1>
          <p>
            Esta primeira parte só prepara a activity, detecta o contexto e deixa a sessão pronta
            para o próximo patch do lobby.
          </p>
        </div>
        <div className={`mode-pill mode-pill--${context.mode}`}>{modeLabel}</div>
      </header>

      <div className="grid">
        <StatusCard
          title="Contexto da sessão"
          subtitle="Leitura inicial da instância aberta no Discord"
        >
          <ul className="kv-list">
            <li><span>Modo</span><strong>{context.mode}</strong></li>
            <li><span>Guild</span><strong>{context.guildId ?? "sem servidor"}</strong></li>
            <li><span>Canal</span><strong>{context.channelId ?? "sem canal"}</strong></li>
            <li><span>Instância</span><strong>{context.instanceId ?? "pendente"}</strong></li>
          </ul>
        </StatusCard>

        <StatusCard
          title="Discord SDK"
          subtitle="Estado de bootstrap da activity"
        >
          <ul className="kv-list">
            <li><span>SDK</span><strong>{state.sdkReady ? "pronto" : "fallback"}</strong></li>
            <li><span>Client ID</span><strong>{state.clientId ?? "não definido"}</strong></li>
            <li><span>Origem</span><strong>{context.source}</strong></li>
          </ul>
        </StatusCard>
      </div>

      <StatusCard title="Próximo passo" subtitle="O patch seguinte entra no fluxo de lobby da própria activity.">
        <p className="plain-copy">{contextText}</p>
      </StatusCard>
    </main>
  );
}
