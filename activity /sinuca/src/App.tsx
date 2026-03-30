import { useEffect, useMemo, useRef, useState } from "react";
import { bootstrapDiscord } from "./sdk/discord";
import type { ActivityBootstrap, RoomSnapshot } from "./types/activity";
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
  currentUser: {
    userId: "local-preview",
    displayName: "Jogador local",
  },
};

type ConnectionState = "connecting" | "connected" | "offline";

function resolveSocketUrl() {
  const configured = import.meta.env.VITE_SINUCA_WS_URL as string | undefined;
  if (configured) return configured;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/api/ws`;
}

export default function App() {
  const [state, setState] = useState<ActivityBootstrap>(initialState);
  const [room, setRoom] = useState<RoomSnapshot | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let mounted = true;
    bootstrapDiscord().then((next) => {
      if (mounted) setState(next);
    });
    return () => {
      mounted = false;
    };
  }, []);

  const instanceId = state.context.instanceId ?? `local-${state.currentUser.userId}`;
  const lobbyLabel = state.context.mode === "server" ? "Lobby com fichas" : "Lobby casual";
  const roomCode = useMemo(() => instanceId.slice(0, 8).toUpperCase(), [instanceId]);

  useEffect(() => {
    const socket = new WebSocket(resolveSocketUrl());
    socketRef.current = socket;
    setConnectionState("connecting");

    socket.addEventListener("open", () => {
      setConnectionState("connected");
      socket.send(JSON.stringify({
        type: "create_room",
        payload: {
          instanceId,
          guildId: state.context.guildId,
          channelId: state.context.channelId,
        },
      }));
      socket.send(JSON.stringify({
        type: "join_room",
        payload: {
          instanceId,
          userId: state.currentUser.userId,
          displayName: state.currentUser.displayName,
        },
      }));
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data as string) as
          | { type: "ready" }
          | { type: "error"; message: string }
          | { type: "room_state"; payload: RoomSnapshot };

        if (payload.type === "error") {
          setErrorMessage(payload.message);
          return;
        }

        if (payload.type === "room_state") {
          setRoom(payload.payload);
          setErrorMessage(null);
        }
      } catch {
        setErrorMessage("resposta inválida do servidor");
      }
    });

    socket.addEventListener("close", () => {
      setConnectionState("offline");
    });

    socket.addEventListener("error", () => {
      setConnectionState("offline");
      setErrorMessage("não foi possível conectar ao servidor da activity");
    });

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [instanceId, state.context.channelId, state.context.guildId, state.currentUser.displayName, state.currentUser.userId]);

  const helperCopy = state.context.mode === "server"
    ? "Aberta em servidor. O lobby já nasce no modo com fichas, mas a cobrança entra no patch seguinte."
    : "Aberta fora de servidor. Aqui a sinuca segue casual e sem economia.";

  return (
    <main className="app-shell">
      <header className="hero-card">
        <div>
          <span className="hero-card__eyebrow">Sinuca Activity</span>
          <h1>Lobby da activity</h1>
          <p>
            A activity agora cria a sala pela instância aberta, entra com o jogador local e já deixa a base
            pronta para o lobby econômico do próximo patch.
          </p>
        </div>
        <div className={`mode-pill mode-pill--${state.context.mode}`}>{lobbyLabel}</div>
      </header>

      <div className="grid grid--tight">
        <StatusCard title="Sessão" subtitle="Contexto detectado ao abrir a activity">
          <ul className="kv-list">
            <li><span>Sala</span><strong>{roomCode}</strong></li>
            <li><span>Modo</span><strong>{state.context.mode}</strong></li>
            <li><span>Conexão</span><strong>{connectionState}</strong></li>
            <li><span>Canal</span><strong>{state.context.channelId ?? "fora de servidor"}</strong></li>
          </ul>
        </StatusCard>

        <StatusCard title="Jogador local" subtitle="Quem entrou automaticamente nesta instância">
          <ul className="kv-list">
            <li><span>Nome</span><strong>{state.currentUser.displayName}</strong></li>
            <li><span>ID</span><strong>{state.currentUser.userId}</strong></li>
            <li><span>SDK</span><strong>{state.sdkReady ? "pronto" : "fallback"}</strong></li>
          </ul>
        </StatusCard>
      </div>

      <StatusCard title="Lobby" subtitle="Base do fluxo que depois vai criar o lobby econômico no servidor.">
        <div className="lobby-card">
          <div>
            <span className="lobby-card__label">Instância</span>
            <strong>{room?.instanceId ?? instanceId}</strong>
          </div>
          <div>
            <span className="lobby-card__label">Jogadores</span>
            <strong>{room?.players.length ?? 1}/2</strong>
          </div>
          <div>
            <span className="lobby-card__label">Contexto</span>
            <strong>{state.context.mode === "server" ? "com fichas" : "casual"}</strong>
          </div>
        </div>

        <ul className="player-list">
          {(room?.players ?? [state.currentUser]).map((player) => (
            <li key={player.userId}>
              <span>{player.displayName}</span>
              <small>{player.userId === state.currentUser.userId ? "você" : "na sessão"}</small>
            </li>
          ))}
          {(room?.players.length ?? 1) < 2 ? <li className="player-list__ghost">Aguardando segundo jogador…</li> : null}
        </ul>

        <p className="plain-copy">{helperCopy}</p>
        {errorMessage ? <p className="error-copy">{errorMessage}</p> : null}
      </StatusCard>
    </main>
  );
}
