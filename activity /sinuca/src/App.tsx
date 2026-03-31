import { useEffect, useMemo, useRef, useState } from "react";
import {
  authorizeDiscordCode,
  authenticateDiscordAccessToken,
  bootstrapDiscord,
  clearCachedToken,
  getDiscordSdk,
  writeCachedToken,
  writeCachedUser,
} from "./sdk/discord";
import type { ActivityBootstrap, BalanceDebugSnapshot, BalanceSnapshot, RoomSnapshot, SessionContextPayload } from "./types/activity";
import StatusCard from "./ui/StatusCard";
import lobbyBackground from "./assets/lobby-bg.png";

const DISCORD_ID_RE = /^\d{17,20}$/;

function isResolvedDiscordUserId(value: string | null | undefined): value is string {
  return typeof value === "string" && DISCORD_ID_RE.test(value);
}

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
    userId: "pending-auth",
    displayName: "Carregando jogador...",
  },
  bootDebug: [],
};

const initialBalance: BalanceSnapshot = {
  chips: 0,
  bonusChips: 0,
};

type ConnectionState = "connecting" | "connected" | "offline";
type AuthState = "checking" | "ready" | "needs_consent";
type LobbyScreen = "home" | "list" | "room";

type IncomingMessage =
  | { type: "ready" }
  | { type: "pong" }
  | { type: "error"; message: string }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] }
  | { type: "balance_state"; payload: BalanceSnapshot }
  | { type: "balance_debug"; payload: BalanceDebugSnapshot }
  | { type: "session_context"; payload: SessionContextPayload }
  | { type: "oauth_token_result"; payload: { ok: boolean; accessToken: string | null; error: string | null; detail: string | null } };

function resolveSocketUrl() {
  const configured = import.meta.env.VITE_SINUCA_WS_URL as string | undefined;
  if (configured) {
    const url = new URL(configured, window.location.origin);
    if (!url.search && window.location.search) url.search = window.location.search;
    return url.toString();
  }
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const base = `${protocol}://${window.location.host}/ws`;
  return window.location.search ? `${base}${window.location.search}` : base;
}

function formatStatus(room: RoomSnapshot) {
  if (room.status === "ready") return "pronta";
  if (room.status === "in_game") return "em jogo";
  return "aguardando";
}

function formatRoomCount(count: number) {
  return count === 1 ? "1 mesa aberta" : `${count} mesas abertas`;
}

function formatBalance(balance: BalanceSnapshot, loaded: boolean) {
  if (!loaded) return "Fichas: --";
  return balance.bonusChips > 0 ? `Fichas: ${balance.chips} + ${balance.bonusChips} bônus` : `Fichas: ${balance.chips}`;
}

export default function App() {
  const [state, setState] = useState<ActivityBootstrap>(initialState);
  const [bootstrapped, setBootstrapped] = useState(false);
  const [room, setRoom] = useState<RoomSnapshot | null>(null);
  const [rooms, setRooms] = useState<RoomSnapshot[]>([]);
  const [screen, setScreen] = useState<LobbyScreen>("home");
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [authBusy, setAuthBusy] = useState(false);
  const [authDebug, setAuthDebug] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [balance, setBalance] = useState<BalanceSnapshot>(initialBalance);
  const [balanceLoaded, setBalanceLoaded] = useState(false);
  const [balanceDebug, setBalanceDebug] = useState<BalanceDebugSnapshot | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const initSentRef = useRef(false);
  const oauthWaiterRef = useRef<((payload: { ok: boolean; accessToken: string | null; error: string | null; detail: string | null }) => void) | null>(null);
  const silentAttemptedRef = useRef(false);

  useEffect(() => {
    let mounted = true;
    bootstrapDiscord().then((next) => {
      if (!mounted) return;
      setState(next);
      setAuthDebug(next.bootDebug.length ? next.bootDebug[next.bootDebug.length - 1] : null);
      setAuthState(isResolvedDiscordUserId(next.currentUser.userId) ? "ready" : "needs_consent");
      setBootstrapped(true);
    }).catch(() => {
      if (!mounted) return;
      setBootstrapped(true);
    });
    return () => {
      mounted = false;
    };
  }, []);

  const instanceId = state.context.instanceId ?? `local-${state.currentUser.userId}`;
  const isServer = state.context.mode === "server";
  const resolvedUser = isResolvedDiscordUserId(state.currentUser.userId);
  const currentPlayer = room?.players.find((player) => player.userId === state.currentUser.userId);
  const canStart = room?.players.length === 2 && room.players.every((player) => player.ready);

  const waitForOAuthTokenResult = () => new Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }>((resolve, reject) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error("socket_offline"));
      return;
    }

    const timeout = window.setTimeout(() => {
      if (oauthWaiterRef.current) oauthWaiterRef.current = null;
      reject(new Error("oauth_timeout"));
    }, 15000);

    oauthWaiterRef.current = (payload) => {
      window.clearTimeout(timeout);
      oauthWaiterRef.current = null;
      resolve(payload);
    };
  });

  const runAuthorizeFlow = async (promptMode: "none" | "consent") => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return { user: null, debug: `authorize:socket_offline:${promptMode}` };
    }

    const authorizeResult = await authorizeDiscordCode(promptMode);
    if (!authorizeResult.code) {
      return { user: null, debug: authorizeResult.debug };
    }

    setAuthDebug(`${authorizeResult.debug}:exchange_ws:start`);
    socket.send(JSON.stringify({
      type: "exchange_token",
      payload: { code: authorizeResult.code },
    }));

    let tokenResult;
    try {
      tokenResult = await waitForOAuthTokenResult();
    } catch (error) {
      return { user: null, debug: `authorize:exchange_ws_exception:${promptMode}:${error instanceof Error ? error.message : "unknown"}` };
    }

    if (!tokenResult.ok || !tokenResult.accessToken) {
      return {
        user: null,
        debug: `authorize:exchange_ws_failed:${promptMode}:${tokenResult.error ?? "unknown"}:${tokenResult.detail ?? "no_detail"}`,
      };
    }

    const discord = getDiscordSdk();
    if (!discord) {
      return { user: null, debug: `authorize:sdk_missing_after_exchange:${promptMode}` };
    }

    const authenticated = await authenticateDiscordAccessToken(discord, tokenResult.accessToken);
    if (!authenticated || !isResolvedDiscordUserId(authenticated.userId)) {
      clearCachedToken();
      return { user: null, debug: `authorize:authenticate_failed:${promptMode}` };
    }

    writeCachedToken(tokenResult.accessToken);
    writeCachedUser(authenticated);
    return { user: authenticated, debug: `authorize:ok:${promptMode}` };
  };

  const handleAuthorize = async () => {
    if (authBusy) return;
    setAuthBusy(true);
    setErrorMessage(null);
    try {
      setAuthDebug("authorize:consent:start");
      const result = await runAuthorizeFlow("consent");
      setAuthDebug(result.debug);
      const user = result.user;
      if (!user || !isResolvedDiscordUserId(user.userId)) {
        setAuthState("needs_consent");
        setErrorMessage("não foi possível confirmar sua conta agora; a activity não recebeu uma identidade válida");
        return;
      }
      setState((current: ActivityBootstrap) => ({
        ...current,
        currentUser: user,
      }));
      setAuthState("ready");
    } catch (error) {
      setAuthDebug(`authorize:exception:${error instanceof Error ? error.message : "unknown"}`);
      setErrorMessage("falha ao abrir o fluxo de autorização");
    } finally {
      setAuthBusy(false);
    }
  };

  useEffect(() => {
    setAuthState(resolvedUser ? "ready" : "needs_consent");
  }, [resolvedUser]);

  const sendMessage = (payload: object) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setErrorMessage("o servidor da activity não está disponível agora");
      return;
    }
    socket.send(JSON.stringify(payload));
  };

  const requestRooms = () => {
    sendMessage({
      type: "list_rooms",
      payload: {
        mode: state.context.mode,
        guildId: state.context.guildId,
        channelId: state.context.channelId,
      },
    });
  };

  const requestBalance = () => {
    if (!isServer || !state.context.guildId || authState !== "ready") return;
    sendMessage({
      type: "get_balance",
      payload: {
        guildId: state.context.guildId,
        userId: resolvedUser ? state.currentUser.userId : null,
      },
    });
  };

  useEffect(() => {
    if (!bootstrapped) return;

    const socket = new WebSocket(resolveSocketUrl());
    socketRef.current = socket;
    initSentRef.current = false;
    oauthWaiterRef.current = null;
    setConnectionState("connecting");

    socket.addEventListener("open", () => {
      setConnectionState("connected");
      setErrorMessage(null);
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data as string) as IncomingMessage;

        if (payload.type === "oauth_token_result") {
          if (oauthWaiterRef.current) oauthWaiterRef.current(payload.payload);
          return;
        }
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
          return;
        }
        if (payload.type === "session_context") {
          setState((current: ActivityBootstrap) => {
            const nextUserId = payload.payload.userId && payload.payload.userId !== "null" && isResolvedDiscordUserId(payload.payload.userId)
              ? payload.payload.userId
              : current.currentUser.userId;
            const nextDisplayName = payload.payload.displayName && payload.payload.displayName !== "null"
              ? payload.payload.displayName
              : current.currentUser.displayName;
            const nextMode = (payload.payload.guildId && payload.payload.guildId !== "null") ? "server" : current.context.mode;
            return {
              ...current,
              context: {
                ...current.context,
                guildId: payload.payload.guildId && payload.payload.guildId !== "null" ? payload.payload.guildId : current.context.guildId,
                channelId: payload.payload.channelId && payload.payload.channelId !== "null" ? payload.payload.channelId : current.context.channelId,
                instanceId: payload.payload.instanceId && payload.payload.instanceId !== "null" ? payload.payload.instanceId : current.context.instanceId,
                mode: nextMode,
              },
              currentUser: {
                userId: nextUserId,
                displayName: nextDisplayName,
              },
            };
          });
          if (payload.payload.userId && payload.payload.userId !== "null" && isResolvedDiscordUserId(payload.payload.userId)) {
            setAuthState("ready");
            setAuthDebug(`ws-session:resolved:${payload.payload.userId}`);
          } else {
            setAuthDebug(`ws-session:pending:${payload.payload.userId ?? "null"}`);
          }
          return;
        }
        if (payload.type === "balance_state") {
          setBalance(payload.payload);
          setBalanceLoaded(true);
          return;
        }
        if (payload.type === "balance_debug") {
          setBalanceDebug(payload.payload);
          console.log("[sinuca balance_debug]", payload.payload);
        }
      } catch {
        setErrorMessage("resposta inválida do servidor");
      }
    });

    socket.addEventListener("close", () => setConnectionState("offline"));
    socket.addEventListener("error", () => {
      setConnectionState("offline");
      setErrorMessage("não foi possível conectar ao servidor da activity");
    });

    return () => {
      if (oauthWaiterRef.current) {
        oauthWaiterRef.current({ ok: false, accessToken: null, error: "socket_closed", detail: null });
      }
      socket.close();
      socketRef.current = null;
    };
  }, [bootstrapped]);

  useEffect(() => {
    if (!bootstrapped || connectionState !== "connected") return;
    if (initSentRef.current) return;
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    socket.send(JSON.stringify({
      type: "init_context",
      payload: {
        userId: resolvedUser ? state.currentUser.userId : null,
        displayName: state.currentUser.displayName,
        guildId: state.context.guildId,
        channelId: state.context.channelId,
        instanceId: state.context.instanceId,
      },
    }));
    initSentRef.current = true;
    requestRooms();
  }, [bootstrapped, connectionState, resolvedUser, state.context.channelId, state.context.guildId, state.context.instanceId, state.currentUser.displayName, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped || connectionState !== "connected") return;
    if (resolvedUser) return;
    if (authBusy) return;
    if (silentAttemptedRef.current) return;

    silentAttemptedRef.current = true;
    setAuthState("checking");
    setAuthDebug("authorize:none:start");

    void (async () => {
      const result = await runAuthorizeFlow("none");
      setAuthDebug(result.debug);
      if (result.user && isResolvedDiscordUserId(result.user.userId)) {
        setState((current: ActivityBootstrap) => ({ ...current, currentUser: result.user! }));
        setAuthState("ready");
        return;
      }
      setAuthState("needs_consent");
    })();
  }, [authBusy, bootstrapped, connectionState, resolvedUser]);

  useEffect(() => {
    if (!bootstrapped || connectionState !== "connected") return;
    if (!state.context.guildId || !resolvedUser) return;
    requestBalance();
  }, [authState, bootstrapped, connectionState, resolvedUser, state.context.guildId, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped || screen !== "list" || connectionState !== "connected") return;
    const interval = window.setInterval(() => requestRooms(), 2500);
    return () => window.clearInterval(interval);
  }, [bootstrapped, connectionState, screen, state.context.channelId, state.context.guildId, state.context.mode]);

  const shouldShowBalanceDebug = isServer && (!balanceLoaded || balance.chips === 0);

  const heroTitle = useMemo(() => {
    if (screen === "list") return "Mesas abertas";
    if (screen === "room") return room ? `Mesa de ${room.hostDisplayName}` : "Sala da partida";
    return "Sinuca 8-ball";
  }, [room, screen]);

  const heroSubtitle = useMemo(() => {
    if (screen === "list") return "Veja as mesas abertas e entre em uma delas.";
    if (screen === "room") return "Aguarde o segundo jogador e marque pronto.";
    return "Crie uma mesa ou entre em uma já aberta.";
  }, [screen]);

  return (
    <main
      className="app-shell"
      style={{ backgroundImage: `linear-gradient(180deg, rgba(4, 10, 17, 0.12), rgba(4, 10, 17, 0.46)), url(${lobbyBackground})` }}
    >
      <header className="hero-card hero-card--compact hero-card--landscape">
        <div className="hero-card__copy">
          <span className="hero-card__eyebrow">Sinuca Activity</span>
          <h1>{heroTitle}</h1>
          <p>{heroSubtitle}</p>
        </div>
        <div className="hero-card__meta">
          {isServer ? <div className="mode-pill mode-pill--server">{formatBalance(balance, balanceLoaded)}</div> : null}
          {isServer ? <div className="mode-pill mode-pill--stake">Entrada: 25 fichas</div> : null}
        </div>
      </header>

      {screen === "home" ? (
        <section className="home-lobby home-lobby--landscape">
          <div className="menu-buttons menu-buttons--home menu-buttons--compact">
            <button
              className="menu-button menu-button--create"
              type="button"
              disabled={authBusy}
              aria-busy={authBusy}
              onClick={() => {
                if (!resolvedUser) {
                  void handleAuthorize();
                  return;
                }
                sendMessage({
                  type: "create_room",
                  payload: {
                    instanceId,
                    guildId: state.context.guildId,
                    channelId: state.context.channelId,
                    mode: state.context.mode,
                    userId: state.currentUser.userId,
                    displayName: state.currentUser.displayName,
                  },
                });
              }}
            >
              <span className="menu-button__eyebrow">Nova mesa</span>
              <strong>{resolvedUser ? "Criar mesa" : authBusy ? "Autorizando conta..." : "Autorizar conta"}</strong>
              <small>{resolvedUser ? "Abra uma mesa nova." : authBusy ? "Confirme a janela de autorização do Discord." : "Toque para confirmar sua conta e liberar as fichas."}</small>
            </button>

            <button
              className="menu-button menu-button--join"
              type="button"
              onClick={() => {
                setScreen("list");
                requestRooms();
              }}
            >
              <span className="menu-button__eyebrow">Mesas abertas</span>
              <strong>Entrar</strong>
              <small>Veja as mesas abertas.</small>
            </button>
          </div>

          <div className="home-footer-strip home-footer-strip--simple">
            <strong>{formatRoomCount(rooms.length)}</strong>
          </div>
        </section>
      ) : null}

      {screen === "list" ? (
        <section className="lobby-panel lobby-panel--compact">
          <div className="toolbar-row toolbar-row--top toolbar-row--single">
            <button className="chip-button" type="button" onClick={() => setScreen("home")}>Voltar</button>
          </div>

          <div className="list-header list-header--simple">
            <div><h2>Mesas abertas</h2></div>
            <div className="list-summary list-summary--single"><strong>{formatRoomCount(rooms.length)}</strong></div>
          </div>

          <div className="room-list-stack">
            {rooms.length === 0 ? (
              <div className="empty-card empty-card--soft">Nenhuma mesa aberta no momento.</div>
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
                    {entry.mode === "server" ? <span>{entry.stakeLabel}</span> : <span>casual</span>}
                  </div>

                  <button
                    className="primary-button"
                    type="button"
                    disabled={authBusy || entry.players.length >= 2}
                    onClick={() => {
                      if (!resolvedUser) {
                        void handleAuthorize();
                        return;
                      }
                      sendMessage({
                        type: "join_room",
                        payload: { roomId: entry.roomId, userId: state.currentUser.userId, displayName: state.currentUser.displayName },
                      });
                    }}
                  >
                    {!resolvedUser ? (authBusy ? "Autorizando..." : "Autorizar") : entry.players.length >= 2 ? "Mesa cheia" : "Entrar"}
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
            <button className="chip-button" type="button" onClick={() => { setRoom(null); setScreen("list"); requestRooms(); }}>Voltar</button>
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
              <div><span className="lobby-card__label">Anfitrião</span><strong>{room.hostDisplayName}</strong></div>
              <div><span className="lobby-card__label">Jogadores</span><strong>{room.players.length}/2</strong></div>
              <div><span className="lobby-card__label">Entrada</span><strong>{room.stakeLabel}</strong></div>
              <div><span className="lobby-card__label">Estado</span><strong>{formatStatus(room)}</strong></div>
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
              {room.players.length < 2 ? <li className="player-list__ghost">Aguardando outro jogador…</li> : null}
            </ul>

            <div className="toolbar-row toolbar-row--end">
              <button
                className="chip-button"
                type="button"
                onClick={() => {
                  sendMessage({ type: "leave_room", payload: { roomId: room.roomId, userId: state.currentUser.userId } });
                  setRoom(null);
                  setScreen("list");
                  requestRooms();
                }}
              >
                Sair
              </button>
              <button
                className={`primary-button ${currentPlayer?.ready ? "primary-button--muted" : ""}`}
                type="button"
                onClick={() => sendMessage({ type: "set_ready", payload: { roomId: room.roomId, userId: state.currentUser.userId, ready: !currentPlayer?.ready } })}
              >
                {currentPlayer?.ready ? "Cancelar pronto" : "Pronto"}
              </button>
            </div>

            <p className="plain-copy">{canStart ? "Os dois jogadores estão prontos." : "A partida começa quando os dois estiverem prontos."}</p>
            {authState === "needs_consent" && !resolvedUser ? <p className="plain-copy">Autorize sua conta para usar fichas, criar mesa e entrar em partida.</p> : null}
            {errorMessage && connectionState !== "offline" ? <p className="error-copy">{errorMessage}</p> : null}
          </StatusCard>
        </>
      ) : null}

      {shouldShowBalanceDebug ? (
        <section className="debug-card">
          <h3>Debug de fichas</h3>
          <div className="debug-grid">
            <span>Boot</span><strong>{state.bootDebug.length ? state.bootDebug.join(" • ") : "sem debug"}</strong>
            <span>Auth</span><strong>{authDebug ?? "sem evento"}</strong>
            <span>Session user</span><strong>{balanceDebug?.sessionUserId ?? state.currentUser.userId ?? "não recebido"}</strong>
            <span>Request user</span><strong>{balanceDebug?.requestUserId ?? state.currentUser.userId ?? "não recebido"}</strong>
            <span>Session guild</span><strong>{balanceDebug?.sessionGuildId ?? state.context.guildId ?? "não recebido"}</strong>
            <span>Request guild</span><strong>{balanceDebug?.requestGuildId ?? state.context.guildId ?? "não recebido"}</strong>
            <span>Mongo</span><strong>{balanceDebug ? (balanceDebug.mongoConnected ? "on" : "off") : "snapshot não recebido"}</strong>
            <span>Query</span><strong>{balanceDebug ? JSON.stringify(balanceDebug.query) : "snapshot não recebido"}</strong>
            <span>Doc</span><strong>{balanceDebug ? (balanceDebug.docFound ? "encontrado" : "não encontrado") : "snapshot não recebido"}</strong>
            <span>Campos</span><strong>{balanceDebug ? (balanceDebug.docKeys.join(", ") || "nenhum") : "snapshot não recebido"}</strong>
            <span>Chips raw</span><strong>{balanceDebug ? String(balanceDebug.rawChips) : "snapshot não recebido"}</strong>
            <span>Bônus raw</span><strong>{balanceDebug ? String(balanceDebug.rawBonusChips) : "snapshot não recebido"}</strong>
            <span>Nota</span><strong>{balanceDebug?.note ?? "snapshot não recebido"}</strong>
          </div>
        </section>
      ) : null}
    </main>
  );
}
