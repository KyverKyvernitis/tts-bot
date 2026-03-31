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
import type { ActivityBootstrap, ActivityUser, BalanceDebugSnapshot, BalanceSnapshot, RoomPlayer, RoomSnapshot, SessionContextPayload } from "./types/activity";
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

type OAuthExchangeResult = { ok: boolean; accessToken: string | null; error: string | null; detail: string | null };

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

function joinBaseAndPath(base: string, path: string) {
  const normalizedBase = base.endsWith("/") ? base.slice(0, -1) : base;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

function resolveApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const configured = (import.meta.env.VITE_SINUCA_API_BASE_URL as string | undefined)?.trim();
  const candidates: string[] = [];

  if (configured) {
    candidates.push(joinBaseAndPath(configured, `/api${normalizedPath}`));
    candidates.push(joinBaseAndPath(configured, normalizedPath));
  }

  candidates.push(`/api${normalizedPath}`);
  candidates.push(normalizedPath);
  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

function resolveSocketUrl() {
  const configured = (import.meta.env.VITE_SINUCA_WS_URL as string | undefined)?.trim();
  if (configured) {
    const url = new URL(configured, window.location.origin);
    if (!url.search && window.location.search) url.search = window.location.search;
    return url.toString();
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socketUrl = new URL(`/ws${window.location.search ?? ""}`, `${protocol}://${window.location.host}`);
  return socketUrl.toString();
}

function formatStatus(room: RoomSnapshot) {
  if (room.status === "ready") return "pronta";
  if (room.status === "in_game") return "em jogo";
  return "aguardando";
}

function formatRoomCount(count: number) {
  return count === 1 ? "1 aberta" : `${count} abertas`;
}

function defaultDiscordAvatarUrl(userId: string) {
  try {
    const index = Number((BigInt(userId) >> 22n) % 6n);
    return `https://cdn.discordapp.com/embed/avatars/${index}.png`;
  } catch {
    return "https://cdn.discordapp.com/embed/avatars/0.png";
  }
}

function buildPlayerTag(player: Pick<RoomPlayer, "displayName">) {
  const label = player.displayName?.trim() || "jogador";
  return label.startsWith("@") ? label : `@${label}`;
}

function resolvePlayerAvatar(player: Pick<RoomPlayer, "userId" | "avatarUrl">) {
  if (player.avatarUrl) return player.avatarUrl;
  return defaultDiscordAvatarUrl(player.userId);
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
  const lastInitKeyRef = useRef<string | null>(null);
  const oauthWaiterRef = useRef<((payload: { ok: boolean; accessToken: string | null; error: string | null; detail: string | null }) => void) | null>(null);
  const balanceReceiptRef = useRef<number>(0);

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

  const waitForOAuthTokenResult = (): Promise<OAuthExchangeResult> => new Promise<OAuthExchangeResult>((resolve, reject) => {
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

  const fetchBalanceOverHttp = async (reason: string) => {
    if (!isServer || !state.context.guildId || !resolvedUser) return false;

    const attempts: string[] = [];
    const candidates = resolveApiCandidates("/balance");
    for (const baseUrl of candidates) {
      const requestUrl = `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}guildId=${encodeURIComponent(state.context.guildId)}&userId=${encodeURIComponent(state.currentUser.userId)}`;
      try {
        const response = await fetch(requestUrl, {
          method: "GET",
          credentials: "same-origin",
        });
        const raw = await response.text();
        let parsed: { balance?: BalanceSnapshot; debug?: BalanceDebugSnapshot; error?: string; detail?: string } | null = null;
        try {
          parsed = raw ? JSON.parse(raw) as { balance?: BalanceSnapshot; debug?: BalanceDebugSnapshot; error?: string; detail?: string } : null;
        } catch {
          parsed = null;
        }

        if (response.ok && parsed?.balance && parsed?.debug) {
          balanceReceiptRef.current = Date.now();
          setBalance(parsed.balance);
          setBalanceLoaded(true);
          setBalanceDebug(parsed.debug);
          setAuthDebug((current) => current ? `${current} • balance:http_ok:${reason}:${baseUrl}:${response.status}` : `balance:http_ok:${reason}:${baseUrl}:${response.status}`);
          return true;
        }

        const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
        attempts.push(`${baseUrl}:${response.status}:${detail}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`${baseUrl}:exception:${message}`);
      }
    }

    if (attempts.length) {
      setAuthDebug((current) => current ? `${current} • balance:http_failed:${reason}:${attempts.join(" | ")}` : `balance:http_failed:${reason}:${attempts.join(" | ")}`);
    }
    return false;
  };

  const exchangeTokenOverHttp = async (code: string): Promise<OAuthExchangeResult> => {
    const baseCandidates = resolveApiCandidates("/token");
    const attempts: string[] = [];
    const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

    for (const baseUrl of baseCandidates) {
      requestVariants.push({
        label: `POST_JSON:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ code }),
        },
      });
      requestVariants.push({
        label: `POST_FORM:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          credentials: "same-origin",
          body: new URLSearchParams({ code }).toString(),
        },
      });
      const getUrl = `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}code=${encodeURIComponent(code)}`;
      requestVariants.push({
        label: `GET_QUERY:${baseUrl}`,
        url: getUrl,
        init: {
          method: "GET",
          credentials: "same-origin",
        },
      });
    }

    for (const variant of requestVariants) {
      try {
        const response = await fetch(variant.url, variant.init);
        const raw = await response.text();
        let parsed: { access_token?: string; error?: string; detail?: string } | null = null;
        try {
          parsed = raw ? JSON.parse(raw) as { access_token?: string; error?: string; detail?: string } : null;
        } catch {
          parsed = null;
        }

        if (response.ok && typeof parsed?.access_token === "string" && parsed.access_token) {
          return {
            ok: true,
            accessToken: parsed.access_token,
            error: null,
            detail: `http_ok:${variant.label}:${response.status}`,
          };
        }

        const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
        attempts.push(`${variant.label}:${response.status}:${detail}`);
        setAuthDebug(`authorize:http_failed:${variant.label}:${response.status}:${detail}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`${variant.label}:exception:${message}`);
        setAuthDebug(`authorize:http_exception:${variant.label}:${message}`);
      }
    }

    return {
      ok: false,
      accessToken: null,
      error: "http_exchange_failed",
      detail: attempts.length ? attempts.join(" | ") : null,
    };
  };

  const runAuthorizeFlow = async (promptMode: "none" | "consent"): Promise<{ user: ActivityUser | null; debug: string }> => {
    const authorizeResult = await authorizeDiscordCode(promptMode);
    if (!authorizeResult.code) {
      return { user: null, debug: authorizeResult.debug };
    }

    setAuthDebug(`${authorizeResult.debug}:exchange_http:start`);
    let tokenResult = await exchangeTokenOverHttp(authorizeResult.code);

    if ((!tokenResult.ok || !tokenResult.accessToken) && socketRef.current?.readyState === WebSocket.OPEN) {
      setAuthDebug(`${authorizeResult.debug}:exchange_ws:fallback`);
      socketRef.current.send(JSON.stringify({
        type: "exchange_token",
        payload: { code: authorizeResult.code },
      }));

      try {
        tokenResult = await waitForOAuthTokenResult();
      } catch (error) {
        return { user: null, debug: `authorize:exchange_failed:${promptMode}:${error instanceof Error ? error.message : "unknown"}` };
      }
    }

    if (!tokenResult.ok || !tokenResult.accessToken) {
      return {
        user: null,
        debug: `authorize:exchange_failed:${promptMode}:${tokenResult.error ?? "unknown"}:${tokenResult.detail ?? "no_detail"}`,
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
      setAuthDebug("authorize:consent:start:user_gesture");
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
      setErrorMessage("falha ao abrir o fluxo de autorização; veja o debug logo abaixo");
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
    if (!isServer || !state.context.guildId || authState !== "ready" || !resolvedUser) return;

    const socket = socketRef.current;
    const requestStartedAt = Date.now();
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      void fetchBalanceOverHttp("socket_unavailable");
      return;
    }

    socket.send(JSON.stringify({
      type: "get_balance",
      payload: {
        guildId: state.context.guildId,
        userId: state.currentUser.userId,
      },
    }));

    window.setTimeout(() => {
      if (balanceReceiptRef.current >= requestStartedAt) return;
      void fetchBalanceOverHttp("ws_timeout_fallback");
    }, 1500);
  };

  useEffect(() => {
    if (!bootstrapped) return;

    const nextSocketUrl = resolveSocketUrl();
    setAuthDebug((current) => current ?? `ws:connecting:${nextSocketUrl}`);
    const socket = new WebSocket(nextSocketUrl);
    socketRef.current = socket;
    lastInitKeyRef.current = null;
    oauthWaiterRef.current = null;
    setConnectionState("connecting");

    socket.addEventListener("open", () => {
      setConnectionState("connected");
      setErrorMessage(null);
      setAuthDebug((current: string | null) => current ? `${current} • ws:open` : "ws:open");
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
          balanceReceiptRef.current = Date.now();
          setBalance(payload.payload);
          setBalanceLoaded(true);
          return;
        }
        if (payload.type === "balance_debug") {
          balanceReceiptRef.current = Date.now();
          setBalanceDebug(payload.payload);
          console.log("[sinuca balance_debug]", payload.payload);
        }
      } catch {
        setErrorMessage("resposta inválida do servidor");
      }
    });

    socket.addEventListener("close", (event) => {
      setConnectionState("offline");
      setAuthDebug((current: string | null) => current ? `${current} • ws:close:${event.code}` : `ws:close:${event.code}`);
    });
    socket.addEventListener("error", () => {
      setConnectionState("offline");
      setErrorMessage("não foi possível conectar ao servidor da activity");
      setAuthDebug((current: string | null) => current ? `${current} • ws:error` : "ws:error");
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
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    const initPayload = {
      userId: resolvedUser ? state.currentUser.userId : null,
      displayName: state.currentUser.displayName,
      guildId: state.context.guildId,
      channelId: state.context.channelId,
      instanceId: state.context.instanceId,
    };
    const nextInitKey = JSON.stringify(initPayload);
    if (lastInitKeyRef.current === nextInitKey) return;

    socket.send(JSON.stringify({
      type: "init_context",
      payload: initPayload,
    }));
    lastInitKeyRef.current = nextInitKey;
    requestRooms();
  }, [bootstrapped, connectionState, resolvedUser, state.context.channelId, state.context.guildId, state.context.instanceId, state.currentUser.displayName, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped) return;
    if (resolvedUser) {
      setAuthState("ready");
      return;
    }
    setAuthState("needs_consent");
    setAuthDebug((current) => current ?? "authorize:waiting_for_user_tap");
  }, [bootstrapped, resolvedUser]);

  useEffect(() => {
    if (!bootstrapped) return;
    if (!state.context.guildId || !resolvedUser) return;
    requestBalance();
  }, [authState, bootstrapped, connectionState, resolvedUser, state.context.guildId, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped || !isServer || !resolvedUser || !state.context.guildId) return;
    if (connectionState === "connected") return;

    void fetchBalanceOverHttp("offline_initial");
    const interval = window.setInterval(() => {
      void fetchBalanceOverHttp("offline_poll");
    }, 5000);
    return () => window.clearInterval(interval);
  }, [bootstrapped, connectionState, isServer, resolvedUser, state.context.guildId, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped || screen !== "list" || connectionState !== "connected") return;
    const interval = window.setInterval(() => requestRooms(), 2500);
    return () => window.clearInterval(interval);
  }, [bootstrapped, connectionState, screen, state.context.channelId, state.context.guildId, state.context.mode]);

  const shouldShowBalanceDebug = isServer && (!balanceLoaded || balance.chips === 0);

  const heroTitle = useMemo(() => {
    if (screen === "list") return "Mesas abertas";
    if (screen === "room") return room ? `Mesa de ${room.hostDisplayName}` : "Sala da partida";
    return "Sinuca de Femboy";
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
          <span className="hero-card__eyebrow">Lobby</span>
          <h1>{heroTitle}</h1>
          <p>{heroSubtitle}</p>
        </div>
        {isServer ? (
          <div className="hero-card__meta hero-card__meta--hud">
            <div className="hero-stat hero-stat--chips">
              <span>Fichas</span>
              <strong>{balanceLoaded ? balance.chips : "..."}</strong>
            </div>
            {balanceLoaded && balance.bonusChips > 0 ? (
              <div className="hero-stat hero-stat--bonus">
                <span>Bônus</span>
                <strong>{balance.bonusChips}</strong>
              </div>
            ) : null}
            <div className="hero-stat hero-stat--entry">
              <span>Entrada</span>
              <strong>25 fichas</strong>
            </div>
          </div>
        ) : null}
      </header>

      {screen === "home" ? (
        <section className="home-lobby home-lobby--landscape home-lobby--streamlined">
          {!resolvedUser ? (
            <div className="menu-buttons menu-buttons--single menu-buttons--compact menu-buttons--hero menu-buttons--hero-late">
              <button
                className="menu-button menu-button--authorize"
                type="button"
                disabled={authBusy}
                aria-busy={authBusy}
                onClick={() => {
                  void handleAuthorize();
                }}
              >
                <span className="menu-button__eyebrow">Conta Discord</span>
                <strong>{authBusy ? "Autorizando conta..." : "Autorizar conta"}</strong>
                <small>{authBusy ? "Confirme a janela de autorização do Discord." : "Autorize para criar mesa, entrar e usar fichas."}</small>
              </button>
            </div>
          ) : (
            <div className="menu-buttons menu-buttons--home menu-buttons--compact menu-buttons--hero menu-buttons--hero-late">
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
                      mode: state.context.mode,
                      userId: state.currentUser.userId,
                      displayName: state.currentUser.displayName,
                      avatarUrl: state.currentUser.avatarUrl ?? null,
                    },
                  });
                }}
              >
                <span className="menu-button__eyebrow">Mesa nova</span>
                <strong>Criar mesa</strong>
                <small>Abra uma mesa.</small>
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
          )}
        </section>
      ) : null}

      {screen === "list" ? (
        <section className="lobby-panel lobby-panel--compact lobby-panel--list">
          <div className="list-topbar">
            <button className="chip-button chip-button--back" type="button" onClick={() => setScreen("home")}>Voltar</button>
            <div className="list-topbar__count">{formatRoomCount(rooms.length)}</div>
          </div>

          <div className="room-list-stack room-list-stack--immersive">
            {rooms.length === 0 ? (
              <div className="empty-card empty-card--soft empty-card--home empty-card--list">
                <strong>Nenhuma mesa aberta</strong>
                <span>Crie uma para começar.</span>
              </div>
            ) : (
              rooms.map((entry) => {
                const host = entry.players.find((player) => player.userId === entry.hostUserId) ?? entry.players[0];
                const opponent = entry.players.find((player) => player.userId !== entry.hostUserId) ?? null;
                return (
                  <article key={entry.roomId} className="room-entry-card room-entry-card--soft room-entry-card--showdown">
                    <div className="room-entry-card__showdown">
                      <div className="participant-slot participant-slot--filled">
                        <div className="participant-slot__avatar-wrap">
                          <img className="participant-slot__avatar" src={resolvePlayerAvatar(host)} alt={host.displayName} />
                        </div>
                        <span className="participant-slot__name">{buildPlayerTag(host)}</span>
                        <small className="participant-slot__role">criador</small>
                      </div>

                      <div className="participant-slot__versus">vs.</div>

                      {opponent ? (
                        <div className="participant-slot participant-slot--filled">
                          <div className="participant-slot__avatar-wrap">
                            <img className="participant-slot__avatar" src={resolvePlayerAvatar(opponent)} alt={opponent.displayName} />
                          </div>
                          <span className="participant-slot__name">{buildPlayerTag(opponent)}</span>
                          <small className="participant-slot__role">jogador</small>
                        </div>
                      ) : (
                        <div className="participant-slot participant-slot--ghost">
                          <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                            <div className="participant-slot__unknown">?</div>
                          </div>
                          <span className="participant-slot__name">Aguardando jogador</span>
                          <small className="participant-slot__role">vaga aberta</small>
                        </div>
                      )}
                    </div>

                    <div className="room-entry-card__footer">
                      <div className="room-entry-card__meta room-entry-card__meta--row">
                        <span>{entry.players.length}/2 jogadores</span>
                        {entry.mode === "server" ? <span>{entry.stakeLabel}</span> : <span>casual</span>}
                        <span className={`status-badge status-badge--${entry.status}`}>{formatStatus(entry)}</span>
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
                            payload: { roomId: entry.roomId, userId: state.currentUser.userId, displayName: state.currentUser.displayName, avatarUrl: state.currentUser.avatarUrl ?? null },
                          });
                        }}
                      >
                        {!resolvedUser ? (authBusy ? "Autorizando..." : "Autorizar") : entry.players.length >= 2 ? "Mesa cheia" : "Entrar"}
                      </button>
                    </div>
                  </article>
                );
              })
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
