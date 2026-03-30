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
type LobbyScreen = "home" | "list" | "room";

type IncomingMessage =
  | { type: "ready" }
  | { type: "pong" }
  | { type: "error"; message: string }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] };

function resolveSocketUrl() {
  const configured = import.meta.env.VITE_SINUCA_WS_URL as string | undefined;
  if (configured) return configured;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/api/ws`;
}

function formatStatus(room: RoomSnapshot) {
  if (room.status === "ready") return "pronta";
  if (room.status === "in_game") return "em jogo";
  return "aguardando";
}

export default function App() {
  const [state, setState] = useState<ActivityBootstrap>(initialState);
  const [room, setRoom] = useState<RoomSnapshot | null>(null);
  const [rooms, setRooms] = useState<RoomSnapshot[]>([]);
  const [screen, setScreen] = useState<LobbyScreen>("home");
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
  const lobbyLabel = state.context.mode === "server" ? "com fichas" : "casual";
  const entryLabel = state.context.mode === "server" ? "Entrada: 25 fichas" : "Partida casual sem fichas";
  const currentPlayer = room?.players.find((player) => player.userId === state.currentUser.userId);
  const canStart = room?.players.length === 2 && room.players.every((player) => player.ready);

  const sendMessage = (payload: object) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setErrorMessage("o servidor da activity não está disponível agora");
      return;
    }
    socket.send(JSON.stringify(payload));
  };

  const refreshRooms = () => {
    sendMessage({
      type: "list_rooms",
      payload: {
        mode: state.context.mode,
        guildId: state.context.guildId,
        channelId: state.context.channelId,
      },
    });
  };

  useEffect(() => {
    const socket = new WebSocket(resolveSocketUrl());
    socketRef.current = socket;
    setConnectionState("connecting");

    socket.addEventListener("open", () => {
      setConnectionState("connected");
      setErrorMessage(null);
      socket.send(
        JSON.stringify({
          type: "list_rooms",
          payload: {
            mode: state.context.mode,
            guildId: state.context.guildId,
            channelId: state.context.channelId,
          },
        }),
      );
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data as string) as IncomingMessage;

        if (payload.type === "error") {
          setErrorMessage(payload.message);
          return;
        }

        if (payload.type === "room_state") {
          setRoom(payload.payload);
          setScreen("room");
          setErrorMessage(null);
          return;
        }

        if (payload.type === "room_list") {
          setRooms(payload.payload);
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
  }, [state.context.channelId, state.context.guildId, state.context.mode]);

  const heroTitle = useMemo(() => {
    if (screen === "list") return "Mesas abertas";
    if (screen === "room") return room ? `Mesa de ${room.hostDisplayName}` : "Sala da partida";
    return "Lobby da sinuca";
  }, [room, screen]);

  const heroSubtitle = useMemo(() => {
    if (screen === "list") return "Veja as mesas abertas no contexto atual e escolha uma para entrar.";
    if (screen === "room") return "Sala pré-partida da mesa 8-ball pool. Os dois jogadores entram aqui antes da partida.";
    return "Escolha entre abrir uma mesa nova ou entrar em uma mesa já criada no contexto atual.";
  }, [screen]);

  return (
    <main className="app-shell">
      <header className="hero-card">
        <div>
          <span className="hero-card__eyebrow">Sinuca Activity</span>
          <h1>{heroTitle}</h1>
          <p>{heroSubtitle}</p>
        </div>
        <div className={`mode-pill mode-pill--${state.context.mode}`}>{lobbyLabel}</div>
      </header>

      <div className="context-strip">
        <span>{entryLabel}</span>
        <strong>{connectionState === "connected" ? "online" : connectionState}</strong>
      </div>

      {screen === "home" ? (
        <section className="action-grid">
          <button
            className="action-card"
            type="button"
            onClick={() => {
              sendMessage({
                type: "create_room",
                payload: {
                  instanceId,
                  guildId: state.context.guildId,
                  channelId: state.context.channelId,
                  userId: state.currentUser.userId,
                  displayName: state.currentUser.displayName,
                },
              });
            }}
          >
            <span className="action-card__eyebrow">8-ball pool</span>
            <strong>Criar partida</strong>
            <small>Abra uma mesa nova e vire o anfitrião da partida.</small>
          </button>

          <button
            className="action-card action-card--secondary"
            type="button"
            onClick={() => {
              setScreen("list");
              refreshRooms();
            }}
          >
            <span className="action-card__eyebrow">Mesas abertas</span>
            <strong>Entrar em partida</strong>
            <small>Veja a lista de mesas abertas e escolha em qual entrar.</small>
          </button>
        </section>
      ) : null}

      {screen === "list" ? (
        <StatusCard title="Lista de mesas" subtitle="Mesas abertas neste contexto da activity.">
          <div className="toolbar-row">
            <button className="chip-button" type="button" onClick={() => setScreen("home")}>Voltar</button>
            <button className="chip-button chip-button--active" type="button" onClick={refreshRooms}>Atualizar</button>
          </div>

          <div className="room-list-grid">
            {rooms.length === 0 ? (
              <div className="empty-card">Nenhuma mesa aberta agora. Crie a primeira partida.</div>
            ) : (
              rooms.map((entry) => (
                <article key={entry.roomId} className="room-entry-card">
                  <div className="room-entry-card__head">
                    <div>
                      <span className="room-entry-card__eyebrow">Mesa aberta</span>
                      <h3>Mesa de {entry.hostDisplayName}</h3>
                    </div>
                    <span className={`status-badge status-badge--${entry.status}`}>{formatStatus(entry)}</span>
                  </div>

                  <ul className="kv-list kv-list--compact">
                    <li><span>Jogadores</span><strong>{entry.players.length}/2</strong></li>
                    <li><span>Modo</span><strong>{entry.mode === "server" ? "com fichas" : "casual"}</strong></li>
                    <li><span>Entrada</span><strong>{entry.stakeLabel}</strong></li>
                  </ul>

                  <button
                    className="primary-button"
                    type="button"
                    disabled={entry.players.length >= 2}
                    onClick={() => {
                      sendMessage({
                        type: "join_room",
                        payload: {
                          roomId: entry.roomId,
                          userId: state.currentUser.userId,
                          displayName: state.currentUser.displayName,
                        },
                      });
                    }}
                  >
                    {entry.players.length >= 2 ? "Mesa cheia" : "Entrar"}
                  </button>
                </article>
              ))
            )}
          </div>
        </StatusCard>
      ) : null}

      {screen === "room" && room ? (
        <>
          <div className="grid grid--tight">
            <StatusCard title="Sessão" subtitle="Contexto detectado ao abrir a activity">
              <ul className="kv-list">
                <li><span>Mesa</span><strong>{room.roomId}</strong></li>
                <li><span>Modo</span><strong>{room.mode === "server" ? "server" : "casual"}</strong></li>
                <li><span>Conexão</span><strong>{connectionState}</strong></li>
                <li><span>Canal</span><strong>{state.context.channelId ?? "fora de servidor"}</strong></li>
              </ul>
            </StatusCard>

            <StatusCard title="Jogador local" subtitle="Quem entrou automaticamente nesta instância">
              <ul className="kv-list">
                <li><span>Nome</span><strong>{state.currentUser.displayName}</strong></li>
                <li><span>ID</span><strong>{state.currentUser.userId}</strong></li>
                <li><span>SDK</span><strong>{state.sdkReady ? "pronto" : "fallback"}</strong></li>
                <li><span>Pronto</span><strong>{currentPlayer?.ready ? "sim" : "não"}</strong></li>
              </ul>
            </StatusCard>
          </div>

          <StatusCard title="Sala da partida" subtitle="Lobby da mesa antes do início da partida de sinuca.">
            <div className="lobby-card lobby-card--room">
              <div>
                <span className="lobby-card__label">Anfitrião</span>
                <strong>{room.hostDisplayName}</strong>
              </div>
              <div>
                <span className="lobby-card__label">Jogadores</span>
                <strong>{room.players.length}/2</strong>
              </div>
              <div>
                <span className="lobby-card__label">Entrada</span>
                <strong>{room.stakeLabel}</strong>
              </div>
              <div>
                <span className="lobby-card__label">Estado</span>
                <strong>{formatStatus(room)}</strong>
              </div>
            </div>

            <ul className="player-list player-list--room">
              {room.players.map((player) => (
                <li key={player.userId}>
                  <div>
                    <span>{player.displayName}</span>
                    <small>{player.userId === room.hostUserId ? "anfitrião" : "jogador"}</small>
                  </div>
                  <strong>{player.ready ? "pronto" : "aguardando"}</strong>
                </li>
              ))}
              {room.players.length < 2 ? <li className="player-list__ghost">Aguardando segundo jogador…</li> : null}
            </ul>

            <div className="toolbar-row toolbar-row--end">
              <button
                className="chip-button"
                type="button"
                onClick={() => {
                  sendMessage({ type: "leave_room", payload: { roomId: room.roomId, userId: state.currentUser.userId } });
                  setRoom(null);
                  setScreen("list");
                  refreshRooms();
                }}
              >
                Sair
              </button>
              <button
                className={`primary-button ${currentPlayer?.ready ? "primary-button--muted" : ""}`}
                type="button"
                onClick={() => sendMessage({
                  type: "set_ready",
                  payload: { roomId: room.roomId, userId: state.currentUser.userId, ready: !currentPlayer?.ready },
                })}
              >
                {currentPlayer?.ready ? "Cancelar pronto" : "Pronto"}
              </button>
            </div>

            <p className="plain-copy">
              {canStart
                ? "Os dois jogadores estão prontos. No próximo patch, essa sala já parte direto para a mesa da partida."
                : "A mesa de sinuca só começa quando os dois jogadores estiverem presentes e marcados como prontos."}
            </p>
            {errorMessage ? <p className="error-copy">{errorMessage}</p> : null}
          </StatusCard>
        </>
      ) : null}
    </main>
  );
}
