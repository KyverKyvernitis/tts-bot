import { useEffect, useMemo, useRef, useState } from "react";
import { bootstrapDiscord } from "./sdk/discord";
import type { ActivityBootstrap, RoomSnapshot } from "./types/activity";
import StatusCard from "./ui/StatusCard";
import lobbyBackground from "./assets/lobby-bg.png";

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
    return "Sinuca 8-ball";
  }, [room, screen]);

  const heroSubtitle = useMemo(() => {
    if (screen === "list") return "Escolha uma mesa aberta para entrar na próxima partida.";
    if (screen === "room") return "Sala pré-partida da mesa antes de abrir a mesa de sinuca.";
    return "Crie uma mesa ou entre em uma partida já aberta no servidor.";
  }, [screen]);

  return (
    <main className="app-shell" style={{ backgroundImage: `linear-gradient(180deg, rgba(3, 11, 18, 0.28), rgba(3, 9, 15, 0.82)), url(${lobbyBackground})` }}>
      <header className="hero-card hero-card--compact">
        <div className="hero-card__copy">
          <span className="hero-card__eyebrow">Sinuca Activity</span>
          <h1>{heroTitle}</h1>
          <p>{heroSubtitle}</p>
        </div>
        <div className="hero-card__meta">
          <div className={`mode-pill mode-pill--${state.context.mode}`}>{lobbyLabel}</div>
          <div className="mode-pill mode-pill--stake">{entryLabel}</div>
        </div>
      </header>

      {screen === "home" ? (
        <section className="home-lobby">
          <div className="menu-buttons menu-buttons--home">
            <button
              className="menu-button menu-button--create"
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
              <span className="menu-button__eyebrow">Nova mesa</span>
              <strong>Criar mesa</strong>
              <small>Abra uma sala nova e assuma a primeira tacada da organização.</small>
            </button>

            <button
              className="menu-button menu-button--join"
              type="button"
              onClick={() => {
                setScreen("list");
                refreshRooms();
              }}
            >
              <span className="menu-button__eyebrow">Mesas abertas</span>
              <strong>Entrar</strong>
              <small>Veja as mesas prontas no servidor e escolha onde jogar.</small>
            </button>
          </div>

          <div className="home-footer-strip">
            <span>{state.context.mode === "server" ? "Servidor com fichas" : "Partida casual"}</span>
            <strong>{rooms.length} mesa{rooms.length === 1 ? "" : "s"} aberta{rooms.length === 1 ? "" : "s"}</strong>
          </div>
        </section>
      ) : null}

      {screen === "list" ? (
        <section className="lobby-panel">
          <div className="toolbar-row toolbar-row--top">
            <button className="chip-button" type="button" onClick={() => setScreen("home")}>Voltar</button>
            <button className="chip-button chip-button--active" type="button" onClick={refreshRooms}>Atualizar</button>
          </div>

          <div className="list-header">
            <div>
              <span className="menu-button__eyebrow">Mesas abertas</span>
              <h2>Escolha uma mesa</h2>
              <p>Entre na próxima partida disponível do contexto atual.</p>
            </div>
            <div className="list-summary">
              <span>{rooms.length} mesa{rooms.length === 1 ? "" : "s"} aberta{rooms.length === 1 ? "" : "s"}</span>
              <strong>{entryLabel}</strong>
            </div>
          </div>

          <div className="room-list-stack">
            {rooms.length === 0 ? (
              <div className="empty-card empty-card--soft">Nenhuma mesa aberta agora. Crie a primeira mesa.</div>
            ) : (
              rooms.map((entry) => (
                <article key={entry.roomId} className="room-entry-card room-entry-card--soft">
                  <div className="room-entry-card__head">
                    <div>
                      <span className="room-entry-card__eyebrow">Mesa aberta</span>
                      <h3>Mesa de {entry.hostDisplayName}</h3>
                    </div>
                    <span className={`status-badge status-badge--${entry.status}`}>{formatStatus(entry)}</span>
                  </div>

                  <div className="room-entry-card__meta">
                    <span>{entry.players.length}/2 jogadores</span>
                    <span>{entry.mode === "server" ? "com fichas" : "casual"}</span>
                    <span>{entry.stakeLabel}</span>
                  </div>

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
        </section>
      ) : null}


      {screen === "room" && room ? (
        <>
          <div className="toolbar-row toolbar-row--top toolbar-row--room">
            <button className="chip-button" type="button" onClick={() => { setRoom(null); setScreen("list"); refreshRooms(); }}>Voltar</button>
          </div>

          <div className="grid grid--tight">
            <StatusCard title="Mesa" subtitle="Resumo da sala antes do início da partida.">
              <ul className="kv-list">
                <li><span>Mesa</span><strong>{room.hostDisplayName}</strong></li>
                <li><span>Modo</span><strong>{room.mode === "server" ? "com fichas" : "casual"}</strong></li>
                <li><span>Entrada</span><strong>{room.stakeLabel}</strong></li>
                <li><span>Canal</span><strong>{state.context.channelId ?? "fora de servidor"}</strong></li>
              </ul>
            </StatusCard>

            <StatusCard title="Seu perfil" subtitle="Jogador que entrou nesta activity">
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
            {errorMessage && connectionState !== "offline" ? <p className="error-copy">{errorMessage}</p> : null}
          </StatusCard>
        </>
      ) : null}
    </main>
  );
}
