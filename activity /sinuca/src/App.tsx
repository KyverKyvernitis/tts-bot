import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useGameController } from "./controllers/useGameController";
import {
  authorizeDiscordCode,
  authenticateDiscordAccessToken,
  bootstrapDiscord,
  clearCachedToken,
  getDiscordSdk,
  writeCachedToken,
  writeCachedUser,
} from "./sdk/discord";
import type { ActivityBootstrap, ActivityUser, AimPointerMode, AimStateSnapshot, BalanceDebugSnapshot, BalanceSnapshot, GameSnapshot, RoomPlayer, RoomSnapshot, SessionContextPayload } from "./types/activity";
import StatusCard from "./ui/StatusCard";
import lobbyBackground from "./assets/lobby-bg.png";
import clickTone from "./assets/mixkit-cool-interface-click-tone-2568_iusvjsoq.wav";
import lobbyBgmAsset from "./assets/lobby-bgm-cat-cafe.mp3";
import GameStage from "./games/GameStage";
import {
  getRealtimeHttpGuardState as getRealtimeHttpGuardStateFromModule,
  isRealtimeSocketHealthy as isRealtimeSocketHealthyFromModule,
  shouldBlockHttpGameDuringRealtime as shouldBlockHttpGameDuringRealtimeFromModule,
  shouldRunHttpGamePolling as shouldRunHttpGamePollingFromModule,
  type RealtimeHttpLockState,
  type SimRecoveryState,
  type SnapshotDebugState,
  type WsGameStateRefState,
} from "./game/bootstrap";
import {
  resetGameRuntimeState as resetGameRuntimeStateFromModule,
  type PendingAimHttpState,
  type ShotDispatchState,
} from "./game/teardown";

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
    avatarUrl: null,
  },
  bootDebug: [],
};

const initialBalance: BalanceSnapshot = {
  chips: 0,
  bonusChips: 0,
};

type ConnectionState = "connecting" | "connected" | "offline";
type AuthState = "checking" | "ready" | "needs_consent";
type LobbyScreen = "home" | "create" | "list" | "room" | "game";
type TableType = "stake" | "casual";

type OAuthExchangeResult = { ok: boolean; accessToken: string | null; error: string | null; detail: string | null };

type IncomingMessage =
  | { type: "ready" }
  | { type: "pong" }
  | { type: "error"; message: string }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] }
  | { type: "game_state"; payload: GameSnapshot }
  | { type: "balance_state"; payload: BalanceSnapshot }
  | { type: "balance_debug"; payload: BalanceDebugSnapshot }
  | { type: "session_context"; payload: SessionContextPayload }
  | { type: "oauth_token_result"; payload: { ok: boolean; accessToken: string | null; error: string | null; detail: string | null } }
  | { type: "aim_state"; payload: AimStateSnapshot }
  | { type: "room_closed"; payload: { roomId: string; reason: string; message: string } };

const DEFAULT_PUBLIC_HOST = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || "osakaagiota.duckdns.org";

function joinBaseAndPath(base: string, path: string) {
  const normalizedBase = base.endsWith("/") ? base.slice(0, -1) : base;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

function resolvePublicBaseCandidates() {
  const configuredApiBase = (import.meta.env.VITE_SINUCA_API_BASE_URL as string | undefined)?.trim();
  const configuredPublicHost = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim();
  const candidates: string[] = [window.location.origin];

  if (configuredApiBase) {
    candidates.push(configuredApiBase);
  }

  const directHost = configuredPublicHost || DEFAULT_PUBLIC_HOST;
  if (directHost) {
    const withScheme = /^https?:\/\//i.test(directHost) ? directHost : `https://${directHost}`;
    candidates.push(withScheme);
  }

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

function resolveApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const candidates: string[] = [`/api${normalizedPath}`];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, `/api${normalizedPath}`));
  }

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, normalizedPath));
  }

  candidates.push(normalizedPath);
  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

function resolveStrictApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiPath = normalizedPath.startsWith("/api/") || normalizedPath === "/api"
    ? normalizedPath
    : `/api${normalizedPath}`;
  const candidates: string[] = [apiPath];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, apiPath));
  }

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

function buildQueryStringFromPayload(payload: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || value === null) continue;
    if (typeof value === "boolean") {
      params.set(key, value ? "true" : "false");
      continue;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) continue;
      params.set(key, `${value}`);
      continue;
    }
    params.set(key, String(value));
  }
  return params.toString();
}

function resolveLegacyBalanceAction(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (normalizedPath === "/rooms") return "rooms_list";
  if (normalizedPath === "/rooms/create") return "room_create";
  if (normalizedPath === "/rooms/join") return "room_join";
  if (normalizedPath === "/rooms/leave") return "room_leave";
  if (normalizedPath === "/rooms/ready") return "room_ready";
  if (normalizedPath === "/rooms/stake") return "room_stake";
  if (normalizedPath === "/games/start") return "game_start";
  if (/^\/rooms\/[^/]+$/.test(normalizedPath)) return "room_get";
  return null;
}

function resolveSocketUrl() {
  const configured = (import.meta.env.VITE_SINUCA_WS_URL as string | undefined)?.trim();
  if (configured) {
    const url = new URL(configured, window.location.origin);
    if (!url.search && window.location.search) url.search = window.location.search;
    return url.toString();
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const relativeSocketUrl = new URL(`/ws${window.location.search ?? ""}`, `${protocol}://${window.location.host}`);

  const configuredPublicHost = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || DEFAULT_PUBLIC_HOST;
  if (configuredPublicHost) {
    const host = configuredPublicHost.replace(/^https?:\/\//i, "").replace(/\/$/, "");
    if (host && host !== window.location.host) {
      return relativeSocketUrl.toString();
    }
  }

  return relativeSocketUrl.toString();
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

function cleanPlayerName(player: Pick<RoomPlayer, "displayName">) {
  const label = player.displayName?.trim() || "jogador";
  return label.replace(/^@+/, "");
}

function resolvePlayerAvatar(player: Pick<RoomPlayer, "userId" | "avatarUrl">) {
  if (player.avatarUrl) return player.avatarUrl;
  return defaultDiscordAvatarUrl(player.userId);
}

const SNAPSHOT_DEBUG_ENABLED = true;
const SNAPSHOT_DEBUG_LOG_EVERY_MS = 450;

function logSnapshotDebug(scope: string, payload: Record<string, unknown>) {
  if (!SNAPSHOT_DEBUG_ENABLED) return;
  console.log(`[sinuca-snapshot-${scope}]`, JSON.stringify(payload));
}


async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit, timeoutMs = 2500) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
}

function appendNoStoreNonce(urlLike: string | URL, nonce?: string) {
  const url = new URL(urlLike.toString(), window.location.origin);
  url.searchParams.set("_rt", nonce ?? `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
  return url;
}


function dispatchLeaveBeacon(roomId: string, userId: string, closeRoom: boolean) {
  const payload = new URLSearchParams();
  payload.set('roomId', roomId);
  payload.set('userId', userId);
  payload.set('closeRoom', String(closeRoom));
  payload.set('reason', closeRoom ? 'activity_unload_close' : 'activity_unload_leave');

  for (const baseUrl of resolveStrictApiCandidates('/rooms/leave')) {
    try {
      if (typeof navigator.sendBeacon === 'function') {
        const blob = new Blob([payload.toString()], { type: 'application/x-www-form-urlencoded;charset=UTF-8' });
        if (navigator.sendBeacon(baseUrl, blob)) return true;
      }
    } catch {
      // ignore and continue with keepalive fallback
    }
  }

  for (const baseUrl of resolveStrictApiCandidates('/rooms/leave')) {
    try {
      void fetch(baseUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
        body: payload.toString(),
        credentials: 'same-origin',
        keepalive: true,
      });
      return true;
    } catch {
      // ignore and keep trying other candidates
    }
  }

  return false;
}

export default function App() {
  const [state, setState] = useState<ActivityBootstrap>(initialState);
  const [bootstrapped, setBootstrapped] = useState(false);
  const [room, setRoom] = useState<RoomSnapshot | null>(null);
  const [game, setGame] = useState<GameSnapshot | null>(null);
  const [remoteAim, setRemoteAim] = useState<AimStateSnapshot | null>(null);
  const [rooms, setRooms] = useState<RoomSnapshot[]>([]);
  const [screen, setScreen] = useState<LobbyScreen>("home");
  const [createTableType, setCreateTableType] = useState<TableType>("stake");
  const [createStake, setCreateStake] = useState<number>(25);
  const [createDraftRoomId, setCreateDraftRoomId] = useState<string | null>(null);
  const [locallyOwnedRoomId, setLocallyOwnedRoomId] = useState<string | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [authBusy, setAuthBusy] = useState(false);
  const [authDebug, setAuthDebug] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [balance, setBalance] = useState<BalanceSnapshot>(initialBalance);
  const [balanceLoaded, setBalanceLoaded] = useState(false);
  const [balanceDebug, setBalanceDebug] = useState<BalanceDebugSnapshot | null>(null);
  const [roomEntryMenuOpen, setRoomEntryMenuOpen] = useState(false);
  const [createEntryMenuOpen, setCreateEntryMenuOpen] = useState(false);
  const [createRoomBusy, setCreateRoomBusy] = useState(false);
  const [gameStartBusy, setGameStartBusy] = useState(false);
  const [gameShootBusy, setGameShootBusy] = useState(false);
  const [roomExitBusy, setRoomExitBusy] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const roomEntryMenuRef = useRef<HTMLDivElement | null>(null);
  const createEntryMenuRef = useRef<HTMLDivElement | null>(null);
  const lastInitKeyRef = useRef<string | null>(null);
  const oauthWaiterRef = useRef<((payload: { ok: boolean; accessToken: string | null; error: string | null; detail: string | null }) => void) | null>(null);
  const balanceReceiptRef = useRef<number>(0);
  const uiClickAudioRef = useRef<HTMLAudioElement | null>(null);
  const lobbyBgmAudioRef = useRef<HTMLAudioElement | null>(null);
  const lobbyBgmFadeTimerRef = useRef<number | null>(null);
  const previousScreenRef = useRef<LobbyScreen>("home");
  const currentScreenRef = useRef<LobbyScreen>("home");
  const currentGameRef = useRef<GameSnapshot | null>(null);
  const createDraftRoomIdRef = useRef<string | null>(null);
  const locallyOwnedRoomIdRef = useRef<string | null>(null);
  const currentRoomRef = useRef<RoomSnapshot | null>(null);
  const isRoomHostRef = useRef(false);
  const currentUserIdRef = useRef<string | null>(null);
  const unloadLeaveSentRef = useRef<string | null>(null);
  const pendingAimHttpRef = useRef<PendingAimHttpState>(null);
  const aimHttpTimerRef = useRef<number | null>(null);
  const lastAimHttpSentAtRef = useRef(0);
  const snapshotDebugRef = useRef<SnapshotDebugState>({ roomId: null, lastReceivedAt: 0, lastLoggedAt: 0, lastRevision: -1, lastSource: null });
  const wsGameStateRef = useRef<WsGameStateRefState>({ roomId: null, lastReceivedAt: 0, shotSequence: 0, revision: 0 });
  const simRecoveryRef = useRef<SimRecoveryState>({ roomId: null, shotSequence: 0, revision: 0, lastProgressAt: 0, lastRecoveryAt: 0, recoveryCount: 0, inFlight: false, lastRequestedShotSequence: 0, lastRequestedRevision: 0 });
  const realtimeHttpLockRef = useRef<RealtimeHttpLockState>({ roomId: null, shotSequence: 0, armedAt: 0, source: null });
  const shotDispatchRef = useRef<ShotDispatchState>({ roomId: null, expectedShotSequence: 0, transport: null, startedAt: 0, reason: null });
  const gameShootBusyRef = useRef(false);

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

  useEffect(() => {
    const audio = new Audio(clickTone);
    audio.preload = "auto";
    audio.volume = 0.14;
    uiClickAudioRef.current = audio;

    return () => {
      uiClickAudioRef.current = null;
      audio.pause();
      audio.src = "";
    };
  }, []);

  useEffect(() => {
    const audio = new Audio(lobbyBgmAsset);
    audio.loop = true;
    audio.preload = "auto";
    audio.volume = 0;
    lobbyBgmAudioRef.current = audio;
    return () => {
      if (lobbyBgmFadeTimerRef.current !== null) {
        window.clearInterval(lobbyBgmFadeTimerRef.current);
        lobbyBgmFadeTimerRef.current = null;
      }
      lobbyBgmAudioRef.current = null;
      audio.pause();
      audio.src = "";
    };
  }, []);

  const fadeLobbyBgmTo = (targetVolume: number, durationMs: number, options?: { pauseWhenDone?: boolean }) => {
    const audio = lobbyBgmAudioRef.current;
    if (!audio) return;
    if (lobbyBgmFadeTimerRef.current !== null) {
      window.clearInterval(lobbyBgmFadeTimerRef.current);
      lobbyBgmFadeTimerRef.current = null;
    }
    const startVolume = audio.volume;
    const delta = targetVolume - startVolume;
    if (Math.abs(delta) < 0.001 || durationMs <= 0) {
      audio.volume = targetVolume;
      if (options?.pauseWhenDone && targetVolume <= 0.001) audio.pause();
      return;
    }
    const startedAt = performance.now();
    lobbyBgmFadeTimerRef.current = window.setInterval(() => {
      const t = Math.min(1, (performance.now() - startedAt) / durationMs);
      const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      audio.volume = Math.max(0, Math.min(0.12, startVolume + delta * eased));
      if (t >= 1) {
        if (lobbyBgmFadeTimerRef.current !== null) {
          window.clearInterval(lobbyBgmFadeTimerRef.current);
          lobbyBgmFadeTimerRef.current = null;
        }
        audio.volume = targetVolume;
        if (options?.pauseWhenDone && targetVolume <= 0.001) audio.pause();
      }
    }, 16);
  };

  const playUiClick = () => {
    const audio = uiClickAudioRef.current;
    if (!audio) return;
    try {
      audio.currentTime = 0;
      void audio.play().catch(() => {});
    } catch {}
  };

  const handleShellClickCapture = (event: MouseEvent<HTMLElement>) => {
    const target = event.target as HTMLElement | null;
    const button = target?.closest("button") as HTMLButtonElement | null;
    if (!button || button.disabled) return;
    playUiClick();
  };

  useEffect(() => {
    const previousScreen = previousScreenRef.current;
    previousScreenRef.current = screen;
    currentScreenRef.current = screen;

    const audio = lobbyBgmAudioRef.current;
    if (!audio) return;

    const inMenuFlow = screen === "home" || screen === "create" || screen === "list" || screen === "room";
    const shouldPlayLobbyBgm = bootstrapped && inMenuFlow && room?.status !== "in_game";

    if (shouldPlayLobbyBgm) {
      if (previousScreen === "game") {
        try { audio.currentTime = 0; } catch {}
      }
      if (audio.paused) {
        void audio.play().catch(() => undefined);
      }
      fadeLobbyBgmTo(0.085, previousScreen === "game" ? 260 : 180);
    } else {
      fadeLobbyBgmTo(0, 180, { pauseWhenDone: true });
    }
  }, [bootstrapped, room?.status, screen]);

  useEffect(() => {
    createDraftRoomIdRef.current = createDraftRoomId;
  }, [createDraftRoomId]);

  useEffect(() => {
    locallyOwnedRoomIdRef.current = locallyOwnedRoomId;
  }, [locallyOwnedRoomId]);

  useEffect(() => {
    currentGameRef.current = game;
  }, [game]);

  useEffect(() => {
    currentRoomRef.current = room;
  }, [room]);

  useEffect(() => {
    if (!game) return;
    setGameShootBusy(false);
  }, [game?.roomId, game?.shotSequence, game?.turnUserId]);

  useEffect(() => {
    isRoomHostRef.current = Boolean(room && room.hostUserId === state.currentUser.userId);
    currentUserIdRef.current = state.currentUser.userId;
  }, [room, state.currentUser.userId]);

  useEffect(() => {
    const flushLeaveOnExit = () => {
      const activeRoom = currentRoomRef.current;
      const userId = currentUserIdRef.current;
      if (!activeRoom || !userId) return;
      const unloadKey = `${activeRoom.roomId}:${userId}:${isRoomHostRef.current ? 'host' : 'guest'}`;
      if (unloadLeaveSentRef.current === unloadKey) return;
      unloadLeaveSentRef.current = unloadKey;
      dispatchLeaveBeacon(activeRoom.roomId, userId, isRoomHostRef.current);
    };

    const handlePageHide = () => {
      flushLeaveOnExit();
    };

    const handleBeforeUnload = () => {
      flushLeaveOnExit();
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        flushLeaveOnExit();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('pagehide', handlePageHide);
    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('pagehide', handlePageHide);
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, []);

  useEffect(() => {
    if (!roomEntryMenuOpen && !createEntryMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (roomEntryMenuRef.current?.contains(target)) return;
      if (createEntryMenuRef.current?.contains(target)) return;
      setRoomEntryMenuOpen(false);
      setCreateEntryMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [createEntryMenuOpen, roomEntryMenuOpen]);

  useEffect(() => {
    if (screen !== "room") setRoomEntryMenuOpen(false);
    if (screen !== "create") setCreateEntryMenuOpen(false);
  }, [screen, room?.roomId]);

  const instanceId = state.context.instanceId ?? `local-${state.currentUser.userId}`;
  const isServer = state.context.mode === "server";
  const resolvedUser = isResolvedDiscordUserId(state.currentUser.userId);
  const serverCreateStakeOptions = [0, 10, 25, 30, 50] as const;
  const dmCreateStakeOptions = [0] as const;
  const createStakeOptions: readonly number[] = isServer ? serverCreateStakeOptions : dmCreateStakeOptions;
  const isFriendlyTable = !isServer || createStake === 0 || createTableType === "casual";
  const canAffordSelectedStake = !isServer || createStake === 0 ? true : balanceLoaded && balance.chips >= createStake;
  const roomHostPlayer = room?.players.find((player) => player.userId === room.hostUserId) ?? room?.players[0] ?? null;
  const roomOpponentPlayer = room?.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const isLocallyOwnedRoom = Boolean(room && locallyOwnedRoomId && room.roomId === locallyOwnedRoomId);
  const isRoomHost = room ? room.hostUserId === state.currentUser.userId || isLocallyOwnedRoom : false;
  const currentPlayer = room?.players.find((player) => player.userId === state.currentUser.userId) ?? (isRoomHost ? roomHostPlayer : null);
  const createPreviewRoom = screen === "create" && room && createDraftRoomId && room.roomId === createDraftRoomId ? room : null;
  const createPreviewHostPlayer = createPreviewRoom?.players.find((player) => player.userId === createPreviewRoom.hostUserId) ?? createPreviewRoom?.players[0] ?? null;
  const createPreviewOpponentPlayer = createPreviewRoom?.players.find((player) => player.userId !== createPreviewRoom.hostUserId) ?? null;
  const canHostStart = Boolean(room && room.players.length === 2 && roomOpponentPlayer?.ready);
  const roomStakeOptions = [0, 10, 25, 30, 50] as const;
  const roomTopStatus = !roomOpponentPlayer
    ? "vaga aberta"
    : canHostStart
      ? "pronta"
      : isRoomHost
        ? "aguardando"
        : currentPlayer?.ready
          ? "você pronto"
          : "aguardando";
  const formatStakeOptionLabel = (stake: number) => stake === 0 ? "Amistoso" : `${stake}`;
  const initReadyForServerActions = !isServer || (resolvedUser && authState === "ready" && Boolean(state.context.guildId) && balanceLoaded);

  useEffect(() => {
    if (screen === "create" && createPreviewRoom) {
      setRoom(createPreviewRoom);
      setLocallyOwnedRoomId(createPreviewRoom.roomId);
      setScreen("room");
    }
  }, [createPreviewRoom, screen]);

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
        const response = await fetchWithTimeout(requestUrl, {
          method: "GET",
          credentials: "same-origin",
        }, 3500);
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
        const response = await fetchWithTimeout(variant.url, variant.init, 4000);
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

  const fetchRoomsOverHttp = async (reason: string) => {
    const attempts: string[] = [];

    for (const baseUrl of resolveStrictApiCandidates("/rooms")) {
      try {
        const url = appendNoStoreNonce(baseUrl, `${Date.now()}`);
        url.searchParams.set("mode", state.context.mode);
        if (state.context.guildId) url.searchParams.set("guildId", state.context.guildId);
        if (state.context.channelId) url.searchParams.set("channelId", state.context.channelId);
        const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { rooms?: RoomSnapshot[]; error?: string } : null;
        if (response.ok && Array.isArray(parsed?.rooms)) {
          setRooms(parsed.rooms);
          setErrorMessage(null);
          setAuthDebug((current) => current ? `${current} • rooms:http_ok:${reason}:api:${baseUrl}` : `rooms:http_ok:${reason}:api:${baseUrl}`);
          return true;
        }
        attempts.push(`API:${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`API:${baseUrl}:exception:${message}`);
      }
    }

    const legacyAction = resolveLegacyBalanceAction("/rooms");
    if (legacyAction) {
      for (const baseUrl of resolveApiCandidates("/balance")) {
        try {
          const url = appendNoStoreNonce(baseUrl, `${Date.now()}`);
          url.searchParams.set("action", legacyAction);
          url.searchParams.set("mode", state.context.mode);
          if (state.context.guildId) url.searchParams.set("guildId", state.context.guildId);
          if (state.context.channelId) url.searchParams.set("channelId", state.context.channelId);
          const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
          const raw = await response.text();
          const parsed = raw ? JSON.parse(raw) as { rooms?: RoomSnapshot[]; error?: string } : null;
          if (response.ok && Array.isArray(parsed?.rooms)) {
            setRooms(parsed.rooms);
            setErrorMessage(null);
            setAuthDebug((current) => current ? `${current} • rooms:http_ok:${reason}:balance:${baseUrl}` : `rooms:http_ok:${reason}:balance:${baseUrl}`);
            return true;
          }
          attempts.push(`BALANCE:${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
        } catch (error) {
          const message = error instanceof Error ? error.message : "unknown";
          attempts.push(`BALANCE:${baseUrl}:exception:${message}`);
        }
      }
    }

    if (attempts.length) {
      setAuthDebug((current) => current ? `${current} • rooms:http_failed:${reason}:${attempts.join(" | ")}` : `rooms:http_failed:${reason}:${attempts.join(" | ")}`);
    }
    return false;
  };

  const fetchRoomStateOverHttp = async (roomId: string, reason: string) => {
    if (shouldBlockHttpAuxDuringRealtime(roomId, "room", reason)) {
      return currentRoomRef.current?.roomId === roomId ? currentRoomRef.current : null;
    }
    const attempts: string[] = [];

    const applyRoomResult = (parsed: { room?: RoomSnapshot | null } | null) => {
      if (parsed?.room) {
        if (parsed.room.status !== "in_game" && (currentScreenRef.current === "game" || currentGameRef.current?.roomId === parsed.room.roomId)) {
          resetGameRuntimeState(parsed.room.roomId, { clearGame: true, reason: `${reason}:room_status_not_in_game` });
        }
        setRoom(parsed.room);
        setCreateDraftRoomId(parsed.room.roomId);
        subscribeRoomRealtime(parsed.room.roomId, `${reason}:http_room_state`);
        if (locallyOwnedRoomIdRef.current === parsed.room.roomId || parsed.room.hostUserId === state.currentUser.userId) {
          setLocallyOwnedRoomId(parsed.room.roomId);
        }
        if (parsed.room.status === "in_game") {
          setScreen("game");
        } else if (currentScreenRef.current === "create" && parsed.room.players.length > 1) {
          setScreen("room");
        }
      } else if (currentScreenRef.current === "room" || currentScreenRef.current === "game") {
        resetGameRuntimeState(roomId, { clearGame: true, reason: `${reason}:room_missing` });
        setRoom(null);
        setCreateDraftRoomId(null);
        setLocallyOwnedRoomId(null);
        setRoomEntryMenuOpen(false);
        setErrorMessage("a sala foi fechada");
        setScreen("list");
        void requestRooms();
      }
      return parsed?.room ?? null;
    };

    for (const baseUrl of resolveStrictApiCandidates(`/rooms/${encodeURIComponent(roomId)}`)) {
      try {
        const requestUrl = appendNoStoreNonce(baseUrl, `${Date.now()}`);
        const response = await fetchWithTimeout(requestUrl.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; error?: string } : null;
        if (response.ok) {
          setAuthDebug((current) => current ? `${current} • room:http_ok:${reason}:api:${baseUrl}` : `room:http_ok:${reason}:api:${baseUrl}`);
          return applyRoomResult(parsed);
        }
        attempts.push(`API:${requestUrl.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`API:${baseUrl}:exception:${message}`);
      }
    }

    const legacyAction = resolveLegacyBalanceAction(`/rooms/${encodeURIComponent(roomId)}`);
    if (legacyAction) {
      for (const baseUrl of resolveApiCandidates("/balance")) {
        try {
          const requestUrl = appendNoStoreNonce(baseUrl, `${Date.now()}`);
          requestUrl.searchParams.set("action", legacyAction);
          requestUrl.searchParams.set("roomId", roomId);
          const response = await fetchWithTimeout(requestUrl.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
          const raw = await response.text();
          const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; error?: string } : null;
          if (response.ok) {
            setAuthDebug((current) => current ? `${current} • room:http_ok:${reason}:balance:${baseUrl}` : `room:http_ok:${reason}:balance:${baseUrl}`);
            return applyRoomResult(parsed);
          }
          attempts.push(`BALANCE:${requestUrl.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
        } catch (error) {
          const message = error instanceof Error ? error.message : "unknown";
          attempts.push(`BALANCE:${baseUrl}:exception:${message}`);
        }
      }
    }

    if (attempts.length) {
      setAuthDebug((current) => current ? `${current} • room:http_failed:${reason}:${attempts.join(" | ")}` : `room:http_failed:${reason}:${attempts.join(" | ")}`);
    }
    return null;
  };

  const postRoomActionOverHttp = async (path: string, payload: Record<string, unknown>, reason: string) => {
    const attempts: string[] = [];
    const query = buildQueryStringFromPayload(payload);
    const legacyAction = resolveLegacyBalanceAction(path);

    const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];
    for (const baseUrl of resolveStrictApiCandidates(path)) {
      requestVariants.push({
        label: `API_POST_JSON:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        },
      });
      requestVariants.push({
        label: `API_POST_FORM:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          credentials: "same-origin",
          body: query,
        },
      });
      const getUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
      if (query) {
        const queryUrl = new URL(getUrl.toString(), window.location.origin);
        const params = new URLSearchParams(query);
        params.forEach((value, key) => queryUrl.searchParams.set(key, value));
        requestVariants.push({
          label: `API_GET_QUERY:${baseUrl}`,
          url: queryUrl.toString(),
          init: {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
          },
        });
      }
    }

    if (legacyAction) {
      for (const baseUrl of resolveApiCandidates("/balance")) {
        const queryUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
        queryUrl.searchParams.set("action", legacyAction);
        const params = new URLSearchParams(query);
        params.forEach((value, key) => queryUrl.searchParams.set(key, value));
        requestVariants.push({
          label: `BALANCE_GET_QUERY:${baseUrl}`,
          url: queryUrl.toString(),
          init: {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
          },
        });
        requestVariants.push({
          label: `BALANCE_POST_FORM:${baseUrl}`,
          url: baseUrl,
          init: {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
            credentials: "same-origin",
            body: buildQueryStringFromPayload({ action: legacyAction, ...payload }),
          },
        });
      }
    }

    for (const variant of requestVariants) {
      try {
        const response = await fetchWithTimeout(variant.url, variant.init, variant.label.startsWith("BALANCE_") ? 3200 : 3500);
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; closed?: boolean; error?: string; detail?: string } : null;
        if (response.ok) {
          setErrorMessage(null);
          setAuthDebug((current) => current ? `${current} • room_action:http_ok:${reason}:${variant.label}` : `room_action:http_ok:${reason}:${variant.label}`);
          return parsed;
        }
        const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
        attempts.push(`${variant.label}:${response.status}:${detail}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`${variant.label}:exception:${message}`);
      }
    }
    if (attempts.length) {
      setAuthDebug((current) => current ? `${current} • room_action:http_failed:${reason}:${attempts.join(" | ")}` : `room_action:http_failed:${reason}:${attempts.join(" | ")}`);
    }
    return null;
  };

  const subscribeRoomRealtime = (roomId: string, reason: string) => {
    if (!roomId) return false;
    const delivered = sendMessage({
      type: "subscribe_room",
      payload: {
        roomId,
        userId: state.currentUser.userId,
      },
    }, { silent: true });
    if (delivered) {
      setAuthDebug((current) => current ? `${current} • room:ws_subscribe:${reason}:${roomId}` : `room:ws_subscribe:${reason}:${roomId}`);
    }
    return delivered;
  };

  const createRoomOverHttp = async (reason: string, override?: { stake: number; tableType: TableType }) => {
    const nextStake = override?.stake ?? createStake;
    const nextTableType = override?.tableType ?? createTableType;
    const result = await postRoomActionOverHttp("/rooms/create", {
      instanceId,
      guildId: state.context.guildId,
      channelId: state.context.channelId,
      mode: state.context.mode,
      userId: state.currentUser.userId,
      displayName: state.currentUser.displayName,
      avatarUrl: state.currentUser.avatarUrl ?? null,
      tableType: isServer ? nextTableType : "casual",
      stakeChips: isServer && nextTableType === "stake" && nextStake > 0 ? nextStake : null,
    }, reason);
    if (result?.room) {
      setRoom(result.room);
      setCreateDraftRoomId(result.room.roomId);
      setLocallyOwnedRoomId(result.room.roomId);
      subscribeRoomRealtime(result.room.roomId, `${reason}:http_created`);
      return result.room;
    }
    return null;
  };

  const joinRoomOverHttp = async (roomId: string, reason: string) => {
    const result = await postRoomActionOverHttp("/rooms/join", {
      roomId,
      userId: state.currentUser.userId,
      displayName: state.currentUser.displayName,
      avatarUrl: state.currentUser.avatarUrl ?? null,
    }, reason);
    if (result?.room) {
      setRoom(result.room);
      setScreen("room");
      if (result.room.hostUserId !== state.currentUser.userId) {
        setLocallyOwnedRoomId(null);
      }
      subscribeRoomRealtime(result.room.roomId, `${reason}:http_joined`);
      return result.room;
    }
    return null;
  };

  const leaveRoomOverHttp = async (roomId: string, reason: string, options?: { closeRoom?: boolean }) => {
    return await postRoomActionOverHttp("/rooms/leave", {
      roomId,
      userId: state.currentUser.userId,
      closeRoom: options?.closeRoom ?? false,
    }, reason);
  };

  const setReadyOverHttp = async (roomId: string, ready: boolean, reason: string) => {
    const result = await postRoomActionOverHttp("/rooms/ready", {
      roomId,
      userId: state.currentUser.userId,
      ready,
    }, reason);
    if (result?.room) {
      setRoom(result.room);
      return result.room;
    }
    return null;
  };

  const updateRoomStakeOverHttp = async (roomId: string, stake: number, reason: string) => {
    const result = await postRoomActionOverHttp("/rooms/stake", {
      roomId,
      userId: state.currentUser.userId,
      stakeChips: stake,
      tableType: stake === 0 ? "casual" : "stake",
    }, reason);
    if (result?.room) {
      setRoom(result.room);
      return result.room;
    }
    return null;
  };


  const isSocketOpen = () => {
    const socket = socketRef.current;
    return Boolean(socket && socket.readyState === WebSocket.OPEN);
  };

  const isRealtimeSocketHealthy = (roomId?: string | null) => isRealtimeSocketHealthyFromModule({
    isSocketOpen: isSocketOpen(),
    roomId,
    wsState: wsGameStateRef.current,
  });

  const markShotDispatch = (roomId: string, expectedShotSequence: number, transport: "ws" | "http", reason: string) => {
    shotDispatchRef.current = {
      roomId,
      expectedShotSequence,
      transport,
      startedAt: performance.now(),
      reason,
    };
  };

  const clearShotDispatch = (roomId?: string | null, reason?: string) => {
    const current = shotDispatchRef.current;
    if (roomId && current.roomId && current.roomId !== roomId) return;
    shotDispatchRef.current = {
      roomId: null,
      expectedShotSequence: 0,
      transport: null,
      startedAt: 0,
      reason: reason ?? null,
    };
  };

  const armRealtimeHttpLock = (roomId: string, shotSequence: number, source: string) => {
    realtimeHttpLockRef.current = { roomId, shotSequence, armedAt: performance.now(), source };
  };

  const clearRealtimeHttpLock = (roomId?: string | null) => {
    const current = realtimeHttpLockRef.current;
    if (roomId && current.roomId && current.roomId !== roomId) return;
    realtimeHttpLockRef.current = { roomId: null, shotSequence: 0, armedAt: 0, source: null };
  };

  const resetGameRuntimeState = (roomId?: string | null, options?: { clearGame?: boolean; reason?: string }) => resetGameRuntimeStateFromModule({
    currentRoomId: currentRoomRef.current?.roomId ?? null,
    currentGameRoomId: currentGameRef.current?.roomId ?? null,
    clearShotDispatch,
    clearRealtimeHttpLock,
    wsGameStateRef,
    simRecoveryRef,
    snapshotDebugRef,
    aimHttpTimerRef,
    pendingAimHttpRef,
    lastAimHttpSentAtRef,
    setRemoteAim,
    setGameShootBusy,
    setGame,
    currentGameRef,
    clearTimeoutFn: window.clearTimeout,
  }, roomId, options);

  const getRealtimeHttpGuardState = (roomId: string) => getRealtimeHttpGuardStateFromModule({
    roomId,
    activeGame: currentGameRef.current,
    activeRoomId: currentRoomRef.current?.roomId ?? null,
    lock: realtimeHttpLockRef.current,
  });

  const shouldBlockHttpGameDuringRealtime = (roomId: string, reason: string) => shouldBlockHttpGameDuringRealtimeFromModule({
    roomId,
    reason,
    activeGame: currentGameRef.current,
    activeRoomId: currentRoomRef.current?.roomId ?? null,
    lock: realtimeHttpLockRef.current,
  });

  const shouldRunHttpGamePolling = (roomId: string) => shouldRunHttpGamePollingFromModule({
    roomId,
    activeGame: currentGameRef.current,
    activeRoomId: currentRoomRef.current?.roomId ?? null,
    lock: realtimeHttpLockRef.current,
    isRealtimeHealthy: isRealtimeSocketHealthy(roomId),
  });

  const shouldBlockHttpAuxDuringRealtime = (roomId: string, kind: "room" | "aim", reason: string) => {
    const guard = getRealtimeHttpGuardState(roomId);
    const activeGame = guard.activeGame;
    const inActiveGameScreen = currentScreenRef.current === 'game' && currentRoomRef.current?.roomId === roomId;
    const shouldBlock = (guard.sameRoom && guard.isRealtimeLocked)
      || (inActiveGameScreen && activeGame?.status !== 'finished')
      || (kind === 'aim' && isRealtimeSocketHealthy(roomId));
    if (!shouldBlock) return false;
    logSnapshotDebug('skip', {
      source: 'http',
      roomId,
      reason,
      status: activeGame?.status ?? null,
      shotSequence: activeGame?.shotSequence ?? guard.lock.shotSequence ?? null,
      revision: activeGame && Number.isFinite(activeGame.snapshotRevision) ? activeGame.snapshotRevision : null,
      why: `${kind}_http_realtime_guard`,
      wsConnected: isSocketOpen(),
      lockSource: guard.lock.source,
    });
    return true;
  };

  const mergeIncomingGame = (current: GameSnapshot | null, incoming: GameSnapshot | null | undefined) => {
    if (!incoming) return current;
    if (current && current.roomId === incoming.roomId) {
      const currentRevision = Number.isFinite(current.snapshotRevision) ? current.snapshotRevision : 0;
      const incomingRevision = Number.isFinite(incoming.snapshotRevision) ? incoming.snapshotRevision : 0;
      if (current.shotSequence > incoming.shotSequence) return current;
      if (current.shotSequence === incoming.shotSequence) {
        if (currentRevision > incomingRevision) return current;
        if (currentRevision === incomingRevision && current.updatedAt > incoming.updatedAt) return current;
      }
    }
    return {
      ...incoming,
      lastShot: incoming.lastShot ?? (current?.roomId === incoming.roomId && current?.shotSequence === incoming.shotSequence ? current.lastShot : null),
    };
  };


  const applyIncomingGame = (
    source: "http" | "ws" | "local",
    incoming: GameSnapshot | null | undefined,
    reason: string,
  ): GameSnapshot | null => {
    if (!incoming) return null;
    if (source === "http" && shouldBlockHttpGameDuringRealtime(incoming.roomId, reason)) {
      logSnapshotDebug('ignore', {
        source,
        roomId: incoming.roomId,
        reason,
        status: incoming.status,
        shotSequence: incoming.shotSequence,
        revision: Number.isFinite(incoming.snapshotRevision) ? incoming.snapshotRevision : 0,
        why: 'blocked_http_game_fetch_during_realtime',
      });
      return null;
    }

    const previousGame = currentGameRef.current;
    const merged = mergeIncomingGame(previousGame, incoming);
    setGame(merged);

    if (merged) {
      currentGameRef.current = merged;
      const now = performance.now();
      const recovery = simRecoveryRef.current;
      const mergedRevision = Number.isFinite(merged.snapshotRevision) ? merged.snapshotRevision : 0;
      const previousRevision = Number.isFinite(previousGame?.snapshotRevision) ? previousGame!.snapshotRevision : -1;
      const advanced = !previousGame
        || previousGame.roomId !== merged.roomId
        || merged.shotSequence > previousGame.shotSequence
        || (merged.shotSequence === previousGame.shotSequence && (mergedRevision > previousRevision || merged.updatedAt > previousGame.updatedAt));

      const shotDispatch = shotDispatchRef.current;
      if (shotDispatch.roomId === merged.roomId && merged.shotSequence >= shotDispatch.expectedShotSequence) {
        clearShotDispatch(merged.roomId, `authoritative_${source}`);
      }

      if (merged.status === 'simulating') {
        armRealtimeHttpLock(merged.roomId, merged.shotSequence, source);
        const sequenceChanged = recovery.roomId !== merged.roomId || recovery.shotSequence !== merged.shotSequence;
        if (sequenceChanged) {
          recovery.roomId = merged.roomId;
          recovery.shotSequence = merged.shotSequence;
          recovery.revision = mergedRevision;
          recovery.lastProgressAt = now;
          recovery.lastRecoveryAt = 0;
          recovery.recoveryCount = 0;
          recovery.inFlight = false;
          recovery.lastRequestedShotSequence = 0;
          recovery.lastRequestedRevision = 0;
        } else if (advanced || source === 'local') {
          recovery.revision = mergedRevision;
          recovery.lastProgressAt = now;
        }
      } else {
        clearRealtimeHttpLock(merged.roomId);
        clearShotDispatch(merged.roomId, `status_${merged.status}`);
        recovery.roomId = merged.roomId;
        recovery.shotSequence = merged.shotSequence;
        recovery.revision = mergedRevision;
        recovery.lastProgressAt = now;
        recovery.lastRecoveryAt = 0;
        recovery.recoveryCount = 0;
        recovery.inFlight = false;
        recovery.lastRequestedShotSequence = 0;
        recovery.lastRequestedRevision = 0;
      }

      logSnapshotDebug('apply', {
        source,
        roomId: merged.roomId,
        reason,
        status: merged.status,
        shotSequence: merged.shotSequence,
        revision: mergedRevision,
      });
    }
    return merged;
  };

  const fetchGameStateOverHttp = async (roomId: string, reason: string, sinceSeq = 0): Promise<GameSnapshot | null> => {
    if (shouldBlockHttpGameDuringRealtime(roomId, reason)) {
      const activeGame = currentGameRef.current;
      logSnapshotDebug('skip', {
        source: 'http',
        roomId,
        reason,
        sinceSeq,
        status: activeGame?.status ?? null,
        shotSequence: activeGame?.shotSequence ?? null,
        revision: Number.isFinite(activeGame?.snapshotRevision) ? activeGame!.snapshotRevision : null,
        why: 'blocked_http_game_fetch_during_realtime',
      });
      return null;
    }

    const isRecoveryRequest = reason.startsWith('force_recover_');
    const recovery = simRecoveryRef.current;
    const activeGame = currentGameRef.current;
    if (isRecoveryRequest) {
      if (recovery.inFlight) {
        logSnapshotDebug('skip', {
          source: 'http',
          roomId,
          reason,
          sinceSeq,
          status: activeGame?.status ?? null,
          shotSequence: activeGame?.shotSequence ?? null,
          revision: Number.isFinite(activeGame?.snapshotRevision) ? activeGame!.snapshotRevision : null,
          why: 'recovery_inflight',
        });
        return null;
      }
      recovery.inFlight = true;
      recovery.lastRecoveryAt = performance.now();
      recovery.lastRequestedShotSequence = activeGame?.roomId === roomId ? activeGame.shotSequence : 0;
      recovery.lastRequestedRevision = activeGame?.roomId === roomId && Number.isFinite(activeGame.snapshotRevision) ? activeGame.snapshotRevision : 0;
    }

    const attempts: string[] = [];
    try {
      for (const baseUrl of resolveApiCandidates(`/games/${roomId}`)) {
        try {
          const url = new URL(baseUrl, window.location.origin);
          if (sinceSeq > 0) url.searchParams.set("sinceSeq", String(sinceSeq));
          const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin" }, 3200);
          const raw = await response.text();
          const parsed = raw ? JSON.parse(raw) as { game?: GameSnapshot | null; error?: string } : null;
          if (response.ok) {
            if (parsed?.game) {
              const debugNow = performance.now();
              const debugState = snapshotDebugRef.current;
              const incomingRevision = Number.isFinite(parsed.game.snapshotRevision) ? parsed.game.snapshotRevision : 0;
              if (debugNow - debugState.lastLoggedAt >= SNAPSHOT_DEBUG_LOG_EVERY_MS || debugState.lastRevision !== incomingRevision || debugState.lastSource !== 'http') {
                logSnapshotDebug('recv', { source: 'http', roomId, status: parsed.game.status, shotSequence: parsed.game.shotSequence, revision: incomingRevision, dtMs: debugState.lastReceivedAt ? Math.round(debugNow - debugState.lastReceivedAt) : null });
                debugState.lastLoggedAt = debugNow;
              }
              debugState.roomId = roomId;
              debugState.lastReceivedAt = debugNow;
              debugState.lastRevision = incomingRevision;
              debugState.lastSource = 'http';
              const applied = applyIncomingGame('http', parsed.game, reason);
              if (applied) setScreen("game");
              return applied;
            }
            return null;
          }
          attempts.push(`${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
        } catch (error) {
          attempts.push(`${baseUrl}:exception:${error instanceof Error ? error.message : "unknown"}`);
        }
      }

      if (attempts.length) {
        setAuthDebug((current) => current ? `${current} • game:http_failed:${reason}:${attempts.join(" | ")}` : `game:http_failed:${reason}:${attempts.join(" | ")}`);
      }
      return null;
    } finally {
      if (isRecoveryRequest) {
        recovery.inFlight = false;
      }
    }
  };

  const pushAimToState = (payload: AimStateSnapshot | null) => {
    if (!payload) return;
    setRemoteAim((current) => {
      if (payload.userId === state.currentUser.userId) return current;
      if (current && current.roomId === payload.roomId && current.userId === payload.userId && payload.seq < current.seq) {
        return current;
      }
      return payload;
    });
  };

  const syncAimOverHttp = async (roomId: string, aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; seq?: number; mode: AimPointerMode }, reason: string) => {
    const payload = {
      roomId,
      userId: state.currentUser.userId,
      visible: aim.visible,
      angle: aim.angle,
      cueX: aim.cueX ?? null,
      cueY: aim.cueY ?? null,
      power: aim.power ?? 0,
      seq: aim.seq ?? 0,
      mode: aim.mode,
    };
    const body = JSON.stringify(payload);
    for (const baseUrl of resolveApiCandidates('/games/aim')) {
      try {
        const response = await fetchWithTimeout(baseUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body,
          keepalive: true,
        }, 1200);
        if (response.ok) return true;
      } catch {}
    }
    setAuthDebug((current) => current ? `${current} • aim_sync_http_failed:${reason}:${roomId}` : `aim_sync_http_failed:${reason}:${roomId}`);
    return false;
  };

  const scheduleAimHttpSync = (roomId: string, aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; seq?: number; mode: AimPointerMode }) => {
    if (isRealtimeSocketHealthy(roomId)) return;
    if (shouldBlockHttpAuxDuringRealtime(roomId, 'aim', 'room_aim_sync')) return;
    pendingAimHttpRef.current = { roomId, aim };
    const flush = () => {
      const pending = pendingAimHttpRef.current;
      if (!pending) return;
      pendingAimHttpRef.current = null;
      lastAimHttpSentAtRef.current = Date.now();
      void syncAimOverHttp(pending.roomId, pending.aim, 'scheduled').catch(() => {});
    };
    const now = Date.now();
    const minGap = aim.mode === 'place' ? 55 : 75;
    const wait = Math.max(0, minGap - (now - lastAimHttpSentAtRef.current));
    if (aimHttpTimerRef.current !== null) window.clearTimeout(aimHttpTimerRef.current);
    if (wait === 0 || !aim.visible || aim.mode === 'idle') {
      flush();
      return;
    }
    aimHttpTimerRef.current = window.setTimeout(() => {
      aimHttpTimerRef.current = null;
      flush();
    }, wait);
  };

  const fetchAimStateOverHttp = async (roomId: string, reason: string) => {
    if (shouldBlockHttpAuxDuringRealtime(roomId, "aim", reason)) {
      return null;
    }
    for (const baseUrl of resolveApiCandidates(`/games/${roomId}/aim`)) {
      try {
        const response = await fetchWithTimeout(baseUrl, { method: 'GET', credentials: 'same-origin' }, 1200);
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { aim?: AimStateSnapshot | null } : null;
        if (response.ok) return parsed?.aim ?? null;
      } catch {}
    }
    setAuthDebug((current) => current ? `${current} • aim_get_http_failed:${reason}:${roomId}` : `aim_get_http_failed:${reason}:${roomId}`);
    return null;
  };

  const postGameActionOverHttp = async (path: string, payload: Record<string, unknown>, reason: string) => {
    const attempts: string[] = [];
    const query = buildQueryStringFromPayload(payload);
    const legacyAction = resolveLegacyBalanceAction(path);
    const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

    for (const baseUrl of resolveApiCandidates(path)) {
      requestVariants.push({
        label: `API_POST_JSON:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        },
      });
      requestVariants.push({
        label: `API_POST_FORM:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          credentials: "same-origin",
          body: query,
        },
      });
      if (query) {
        const getUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
        const queryUrl = new URL(getUrl.toString(), window.location.origin);
        const params = new URLSearchParams(query);
        params.forEach((value, key) => queryUrl.searchParams.set(key, value));
        requestVariants.push({
          label: `API_GET_QUERY:${baseUrl}`,
          url: queryUrl.toString(),
          init: {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
          },
        });
      }
    }

    if (legacyAction) {
      for (const baseUrl of resolveApiCandidates("/balance")) {
        const queryUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
        queryUrl.searchParams.set("action", legacyAction);
        const params = new URLSearchParams(query);
        params.forEach((value, key) => queryUrl.searchParams.set(key, value));
        requestVariants.push({
          label: `BALANCE_GET_QUERY:${baseUrl}`,
          url: queryUrl.toString(),
          init: {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
          },
        });
        requestVariants.push({
          label: `BALANCE_POST_FORM:${baseUrl}`,
          url: baseUrl,
          init: {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
            credentials: "same-origin",
            body: buildQueryStringFromPayload({ action: legacyAction, ...payload }),
          },
        });
      }
    }

    for (const variant of requestVariants) {
      try {
        console.log("[sinuca-http-action]", JSON.stringify({ path, label: variant.label, url: variant.url, reason, payload }));
        const response = await fetchWithTimeout(variant.url, variant.init, variant.label.startsWith("BALANCE_") ? 3200 : 4200);
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { game?: GameSnapshot | null; room?: RoomSnapshot | null; error?: string; detail?: string } : null;
        if (response.ok) {
          setAuthDebug((current) => current ? `${current} • game_action:http_ok:${reason}:${variant.label}` : `game_action:http_ok:${reason}:${variant.label}`);
          return parsed;
        }
        const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
        attempts.push(`${variant.label}:${response.status}:${detail}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`${variant.label}:exception:${message}`);
      }
    }

    if (attempts.length) {
      setAuthDebug((current) => current ? `${current} • game_action:http_failed:${reason}:${attempts.join(" | ")}` : `game_action:http_failed:${reason}:${attempts.join(" | ")}`);
    }
    return null;
  };

  const startGameOverHttp = async (roomId: string, reason: string, overrideUserId?: string | null) => {
    const result = await postGameActionOverHttp("/games/start", {
      roomId,
      userId: overrideUserId ?? state.currentUser.userId,
    }, reason);
    if (result?.room) setRoom(result.room);
    if (result?.game) {
      currentGameRef.current = result.game;
      setGame(result.game);
      setScreen("game");
      return result.game;
    }

    const refreshedRoom = await fetchRoomStateOverHttp(roomId, `${reason}:verify_room_after_start`);
    if (refreshedRoom) {
      setRoom(refreshedRoom);
    }

    const recoveredGame = await fetchGameStateOverHttp(roomId, `${reason}:verify_game_after_start`, 0);
    if (recoveredGame) {
      const applied = applyIncomingGame('http', recoveredGame, `${reason}:verify_game_after_start`);
      setScreen("game");
      return applied;
    }
    return null;
  };

  const shootGameOverHttp = async (roomId: string, shot: { angle: number; power: number; cueX?: number | null; cueY?: number | null; calledPocket?: number | null; spinX?: number | null; spinY?: number | null }, reason: string) => {
    const previousSeq = game?.roomId === roomId ? game.shotSequence : 0;
    markShotDispatch(roomId, previousSeq + 1, 'http', reason);
    const payload = {
      roomId,
      userId: state.currentUser.userId,
      angle: shot.angle,
      power: shot.power,
      cueX: shot.cueX ?? null,
      cueY: shot.cueY ?? null,
      calledPocket: shot.calledPocket ?? null,
      spinX: shot.spinX ?? 0,
      spinY: shot.spinY ?? 0,
    };

    console.log("[sinuca-shoot-dispatch]", JSON.stringify({ reason, previousSeq, payload }));
    let result = await postGameActionOverHttp("/games/shoot", payload, reason);
    if (result?.game) {
      return applyIncomingGame('http', result.game, `${reason}:post_result`);
    }

    const refreshedAfterPost = await fetchGameStateOverHttp(roomId, `${reason}:verify_after_post`, previousSeq);
    if (refreshedAfterPost && refreshedAfterPost.shotSequence > previousSeq) {
      return refreshedAfterPost;
    }

    console.warn("[sinuca-shoot-missing]", JSON.stringify({ roomId, previousSeq, reason }));
    clearShotDispatch(roomId, 'http_missing');
    setErrorMessage("A tacada não chegou ao servidor.");
    return null;
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

    const authenticated = await authenticateDiscordAccessToken(discord, tokenResult.accessToken, state.context.guildId);
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

  useEffect(() => {
    if (!isServer) {
      setCreateTableType("casual");
      setCreateStake(0);
      return;
    }
    setCreateTableType("stake");
    setCreateStake(25);
  }, [isServer]);

  useEffect(() => {
    if (screen !== "create") return;
    if (!isServer) {
      setCreateTableType("casual");
      setCreateStake(0);
      return;
    }
    setCreateTableType((current) => current === "stake" || current === "casual" ? current : "stake");
    setCreateStake((current) => createStakeOptions.includes(current) ? current : 25);
  }, [isServer, screen]);

  useEffect(() => {
    if (!bootstrapped || screen !== "create") return;

    let cancelled = false;
    const ensureDraftRoom = async (reason: string) => {
      if (cancelled) return;
      const nextRoom = await createRoomOverHttp(reason);
      if (!cancelled && nextRoom) {
        setCreateDraftRoomId(nextRoom.roomId);
      }
    };

    void ensureDraftRoom("http_primary_create");
    const interval = window.setInterval(() => {
      if (!createDraftRoomIdRef.current) {
        void ensureDraftRoom("http_create_retry");
      }
    }, 1500);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [bootstrapped, createStake, instanceId, screen, state.context.channelId, state.context.guildId, state.context.mode, state.currentUser.avatarUrl, state.currentUser.displayName, state.currentUser.userId]);


  const sendMessage = (payload: object, options?: { silent?: boolean }) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      if (!options?.silent) setErrorMessage("o servidor da activity não está disponível agora");
      return false;
    }
    socket.send(JSON.stringify(payload));
    return true;
  };

  useEffect(() => {
    gameShootBusyRef.current = gameShootBusy;
  }, [gameShootBusy]);

  useEffect(() => {
    if (screen !== "game" || !room || !game) {
      setRemoteAim(null);
      return;
    }
    if (game.turnUserId === state.currentUser.userId || game.status === "finished") {
      setRemoteAim(null);
    }
  }, [game?.roomId, game?.shotSequence, game?.status, game?.turnUserId, room?.roomId, screen, state.currentUser.userId]);

  useEffect(() => {
    if (screen !== 'game' || !room || !game || game.status === 'finished' || game.status === 'simulating' || game.turnUserId === state.currentUser.userId) return;
    if (connectionState === 'connected') return;
    let cancelled = false;
    const tick = async () => {
      const aim = await fetchAimStateOverHttp(room.roomId, 'poll');
      if (cancelled) return;
      if (aim && aim.roomId === room.roomId && aim.userId !== state.currentUser.userId && aim.visible && aim.mode !== 'idle') {
        pushAimToState(aim);
      } else if (aim && aim.roomId === room.roomId && aim.userId !== state.currentUser.userId && !aim.visible) {
        setRemoteAim((current) => current?.roomId === room.roomId ? null : current);
      }
    };
    void tick();
    const interval = window.setInterval(() => { void tick(); }, 90);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [connectionState, game?.roomId, game?.shotSequence, game?.status, game?.turnUserId, room?.roomId, screen, state.currentUser.userId]);

  useEffect(() => () => {
    if (aimHttpTimerRef.current !== null) {
      window.clearTimeout(aimHttpTimerRef.current);
      aimHttpTimerRef.current = null;
    }
  }, []);

  const requestRooms = async () => {
    console.log("[sinuca-ui-request-rooms]", {
      mode: state.context.mode,
      guildId: state.context.guildId,
      channelId: state.context.channelId,
      screen,
    });
    const deliveredOverSocket = sendMessage({
      type: "list_rooms",
      payload: {
        mode: state.context.mode,
        guildId: state.context.guildId,
        channelId: state.context.channelId,
      },
    }, { silent: true });
    if (deliveredOverSocket) return;
    await fetchRoomsOverHttp("http_primary_list");
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
          if (payload.payload.status !== "in_game" && (currentScreenRef.current === "game" || currentGameRef.current?.roomId === payload.payload.roomId)) {
            resetGameRuntimeState(payload.payload.roomId, { clearGame: true, reason: 'ws_room_state_not_in_game' });
          }
          setRoom(payload.payload);
          setCreateDraftRoomId(payload.payload.roomId);
          if (locallyOwnedRoomIdRef.current === payload.payload.roomId || payload.payload.hostUserId === currentUserIdRef.current) {
            setLocallyOwnedRoomId(payload.payload.roomId);
          }
          if (payload.payload.status === "in_game") {
            setScreen("game");
          } else if (currentScreenRef.current !== "create" || payload.payload.players.length > 1) {
            setScreen("room");
          }
          setErrorMessage(null);
          return;
        }
        if (payload.type === "room_closed") {
          const activeRoomId = currentRoomRef.current?.roomId ?? createDraftRoomIdRef.current;
          if (activeRoomId !== payload.payload.roomId) return;
          const wasHost = currentRoomRef.current?.hostUserId === currentUserIdRef.current || locallyOwnedRoomIdRef.current === payload.payload.roomId;
          unloadLeaveSentRef.current = null;
          setRoomEntryMenuOpen(false);
          setCreateEntryMenuOpen(false);
          setCreateDraftRoomId(null);
          setLocallyOwnedRoomId((current) => current === payload.payload.roomId ? null : current);
          resetGameRuntimeState(payload.payload.roomId, { clearGame: true, reason: `ws_room_closed_${payload.payload.reason}` });
          setRoom(null);
          setScreen(wasHost ? "home" : "list");
          setErrorMessage(payload.payload.message);
          void requestRooms();
          return;
        }
        if (payload.type === "game_state") {
          const debugNow = performance.now();
          const debugState = snapshotDebugRef.current;
          const incomingRevision = Number.isFinite(payload.payload.snapshotRevision) ? payload.payload.snapshotRevision : 0;
          if (debugNow - debugState.lastLoggedAt >= SNAPSHOT_DEBUG_LOG_EVERY_MS || debugState.lastRevision !== incomingRevision || debugState.lastSource !== 'ws') {
            logSnapshotDebug('recv', { source: 'ws', roomId: payload.payload.roomId, status: payload.payload.status, shotSequence: payload.payload.shotSequence, revision: incomingRevision, dtMs: debugState.lastReceivedAt ? Math.round(debugNow - debugState.lastReceivedAt) : null });
            debugState.lastLoggedAt = debugNow;
          }
          debugState.roomId = payload.payload.roomId;
          debugState.lastReceivedAt = debugNow;
          debugState.lastRevision = incomingRevision;
          debugState.lastSource = 'ws';
          const wsState = wsGameStateRef.current;
          wsState.roomId = payload.payload.roomId;
          wsState.lastReceivedAt = debugNow;
          wsState.shotSequence = payload.payload.shotSequence;
          wsState.revision = incomingRevision;
          const recovery = simRecoveryRef.current;
          if (recovery.roomId === payload.payload.roomId && (payload.payload.shotSequence > recovery.lastRequestedShotSequence || (payload.payload.shotSequence === recovery.lastRequestedShotSequence && incomingRevision > recovery.lastRequestedRevision))) {
            recovery.inFlight = false;
            recovery.lastProgressAt = debugNow;
          }
          applyIncomingGame('ws', payload.payload, 'ws_game_state');
          setRemoteAim((current) => current?.roomId === payload.payload.roomId && payload.payload.turnUserId !== state.currentUser.userId && payload.payload.status !== "finished" ? current : null);
          setScreen("game");
          setErrorMessage(null);
          return;
        }
        if (payload.type === "aim_state") {
          pushAimToState(payload.payload);
          return;
        }
        if (payload.type === "room_list") {
          setRooms(payload.payload);
          const activeRoomId = currentRoomRef.current?.roomId ?? createDraftRoomIdRef.current;
          if (activeRoomId && currentScreenRef.current !== "game") {
            const matchingRoom = payload.payload.find((entry) => entry.roomId === activeRoomId) ?? null;
            if (matchingRoom) {
              if (matchingRoom.status !== "in_game" && currentGameRef.current?.roomId === matchingRoom.roomId) {
                resetGameRuntimeState(matchingRoom.roomId, { clearGame: true, reason: 'ws_room_list_not_in_game' });
              }
              setRoom(matchingRoom);
              setCreateDraftRoomId(matchingRoom.roomId);
              if (locallyOwnedRoomIdRef.current === matchingRoom.roomId || matchingRoom.hostUserId === currentUserIdRef.current) {
                setLocallyOwnedRoomId(matchingRoom.roomId);
              }
              if (matchingRoom.status === "in_game") {
                setScreen("game");
              } else if (currentScreenRef.current !== "create" || matchingRoom.players.length > 1) {
                setScreen("room");
              }
            }
          }
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
                ...current.currentUser,
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
    void requestRooms();
  }, [bootstrapped, connectionState, resolvedUser, state.context.channelId, state.context.guildId, state.context.instanceId, state.currentUser.displayName, state.currentUser.userId]);

  useEffect(() => {
    if (!bootstrapped || connectionState !== "connected") return;
    if (screen === "game") return;
    const activeRoomId = screen === "room"
      ? room?.roomId ?? null
      : screen === "create"
        ? createDraftRoomIdRef.current ?? createDraftRoomId
        : null;
    if (!activeRoomId) return;
    subscribeRoomRealtime(activeRoomId, `effect_${screen}`);
  }, [bootstrapped, connectionState, createDraftRoomId, room?.roomId, screen, state.currentUser.userId]);

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
    if (!bootstrapped || !isServer || !resolvedUser || !state.context.guildId || balanceLoaded) return;

    let cancelled = false;
    const pumpBalance = async () => {
      if (cancelled) return;
      requestBalance();
      await fetchBalanceOverHttp("online_retry");
    };

    void pumpBalance();
    const interval = window.setInterval(() => {
      void pumpBalance();
    }, 2500);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [balanceLoaded, bootstrapped, isServer, resolvedUser, state.context.guildId, state.currentUser.userId]);

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
    if (!bootstrapped || screen !== "list") return;
    void requestRooms();
    const interval = window.setInterval(() => {
      void requestRooms();
    }, 2500);
    return () => window.clearInterval(interval);
  }, [bootstrapped, connectionState, screen, state.context.channelId, state.context.guildId, state.context.mode]);

  useEffect(() => {
    if (!bootstrapped) return;
    if (screen === "game") return;
    const activeRoomId = screen === "room"
      ? room?.roomId ?? null
      : screen === "create"
        ? createDraftRoomIdRef.current ?? createDraftRoomId
        : null;
    if (!activeRoomId) return;

    const isConnected = connectionState === "connected";
    void fetchRoomStateOverHttp(activeRoomId, isConnected ? "connected_room_sync_initial" : "offline_initial");

    const interval = window.setInterval(() => {
      void fetchRoomStateOverHttp(activeRoomId, isConnected ? "connected_room_sync_fallback" : "offline_poll");
    }, isConnected ? 2000 : 2500);

    return () => window.clearInterval(interval);
  }, [bootstrapped, connectionState, createDraftRoomId, room?.roomId, screen]);





  useEffect(() => {
    if (screen !== "game") return;
    if (!game) return;
    if (shotDispatchRef.current.roomId === game.roomId && game.shotSequence >= shotDispatchRef.current.expectedShotSequence) {
      clearShotDispatch(game.roomId, 'game_effect_advanced');
    }
    setGameShootBusy(false);
  }, [game?.shotSequence, game?.turnUserId, game?.updatedAt, screen]);



  const exitCurrentRoom = async (reason: string) => {
    if (!room || roomExitBusy) return;
    setRoomExitBusy(true);
    setErrorMessage(null);

    try {
      const result = await leaveRoomOverHttp(room.roomId, reason, { closeRoom: isRoomHost });
      if (result === null) {
        setErrorMessage(isRoomHost ? "não foi possível fechar a sala agora" : "não foi possível sair da sala agora");
        return;
      }

      unloadLeaveSentRef.current = null;
      setRoomEntryMenuOpen(false);
      setCreateEntryMenuOpen(false);
      setCreateDraftRoomId(null);
      resetGameRuntimeState(room.roomId, { clearGame: true, reason });
      setRoom(null);
      setScreen(isRoomHost ? "home" : "list");
      void requestRooms();
    } finally {
      setRoomExitBusy(false);
    }
  };

  const shouldShowBalanceDebug = Boolean(import.meta.env.DEV) && isServer && (!balanceLoaded || balance.chips === 0);

  const heroTitle = useMemo(() => {
    if (screen === "create") return "Criar mesa";
    if (screen === "list") return "Mesas abertas";
    if (screen === "room") {
      if (!roomOpponentPlayer) return "Mesa aberta";
      if (isRoomHost) return canHostStart ? "Mesa pronta" : "Mesa aberta";
      return currentPlayer?.ready ? "Pronto" : "Mesa encontrada";
    }
    if (screen === "game") return "Mesa em jogo";
    return "Sinuca de Femboy";
  }, [canHostStart, currentPlayer?.ready, isRoomHost, roomOpponentPlayer, screen]);

  const heroSubtitle = useMemo(() => {
    if (screen === "create") return "Abra a mesa e ajuste a entrada.";
    if (screen === "list") return "Entre em uma mesa aberta.";
    if (screen === "room") {
      if (!room) return "Acompanhe a mesa.";
      if (isRoomHost) {
        if (!roomOpponentPlayer) return "Aguardando jogador.";
        return canHostStart ? "Pronta para iniciar." : "Esperando pronto.";
      }
      return currentPlayer?.ready ? "Aguardando início." : "Marque pronto.";
    }
    if (screen === "game") {
      if (!game) return "Carregando a mesa.";
      return game.turnUserId === state.currentUser.userId ? "Sua vez de bater." : "Aguardando a jogada do adversário.";
    }
    return "Crie ou entre em uma mesa.";
  }, [canHostStart, currentPlayer?.ready, isRoomHost, room, roomOpponentPlayer, screen]);

  const heroEyebrow = useMemo(() => {
    if (screen === "create") return "Mesa nova";
    if (screen === "list") return "Salas";
    if (screen === "room") return "Sala";
    if (screen === "game") return "Partida";
    return "Lobby";
  }, [screen]);

  const heroSecondaryLabel = useMemo(() => {
    if (!isServer) return null;
    if (screen === "create") {
      return { label: "Entrada", value: formatStakeOptionLabel(createStake) };
    }
    if (screen === "room" && room) {
      return { label: "Entrada", value: formatStakeOptionLabel(room.tableType === "stake" ? (room.stakeChips ?? 0) : 0) };
    }
    if (screen === "game" && room) {
      return { label: "Entrada", value: formatStakeOptionLabel(room.tableType === "stake" ? (room.stakeChips ?? 0) : 0) };
    }
    return null;
  }, [createStake, isServer, room, screen]);

  const heroEntryEditable = Boolean(isServer && heroSecondaryLabel && (screen === "create" || (screen === "room" && room && isRoomHost)));

  const { gameLoadingTimedOut, forceReturnToLobbyFromLoading } = useGameController({
    bootstrapped,
    screen,
    room,
    game,
    roomExitBusy,
    isRoomHost,
    currentUserId: state.currentUser.userId,
    currentGameRef,
    currentRoomRef,
    currentScreenRef,
    locallyOwnedRoomIdRef,
    unloadLeaveSentRef,
    simRecoveryRef,
    wsGameStateRef,
    setRoomExitBusy,
    setErrorMessage,
    setRoomEntryMenuOpen,
    setCreateEntryMenuOpen,
    setCreateDraftRoomId,
    setLocallyOwnedRoomId,
    setRoom,
    setGameStartBusy,
    setScreen,
    requestRooms,
    leaveRoomOverHttp,
    dispatchLeaveBeacon,
    resetGameRuntimeState,
    logSnapshotDebug,
    isSocketOpen,
    shouldRunHttpGamePolling,
    fetchGameStateOverHttp,
  });

  return (
    <main
      className={`app-shell ${screen === "game" ? "app-shell--game" : ""}`}
      style={{ backgroundImage: `linear-gradient(180deg, rgba(4, 10, 17, 0.12), rgba(4, 10, 17, 0.46)), url(${lobbyBackground})` }}
      onClickCapture={handleShellClickCapture}
    >
      {screen !== "game" ? (
      <header className={`hero-card hero-card--compact hero-card--landscape ${(createEntryMenuOpen || roomEntryMenuOpen) ? "hero-card--menu-open" : ""}`}>
        <div className="hero-card__copy">
          <span className="hero-card__eyebrow">{heroEyebrow}</span>
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
            {heroSecondaryLabel ? (
              heroEntryEditable ? (
                <div
                  ref={screen === "create" ? createEntryMenuRef : roomEntryMenuRef}
                  className={`entry-selector entry-selector--hero entry-selector--hero-compact ${screen === "create" ? (createEntryMenuOpen ? "entry-selector--open" : "") : (roomEntryMenuOpen ? "entry-selector--open" : "")}`}
                >
                  <button
                    className="entry-selector__trigger entry-selector__trigger--hero"
                    type="button"
                    onClick={() => {
                      if (screen === "create") {
                        setCreateEntryMenuOpen((current) => !current);
                        setRoomEntryMenuOpen(false);
                        return;
                      }
                      setRoomEntryMenuOpen((current) => !current);
                      setCreateEntryMenuOpen(false);
                    }}
                  >
                    <span className="entry-selector__trigger-copy">
                      <span className="entry-selector__label">{heroSecondaryLabel.label}</span>
                      <strong>{heroSecondaryLabel.value}</strong>
                    </span>
                    <span className={`entry-selector__chevron ${(screen === "create" ? createEntryMenuOpen : roomEntryMenuOpen) ? "entry-selector__chevron--open" : ""}`}>v</span>
                  </button>
                  <div className={`entry-selector__menu entry-selector__menu--hero ${(screen === "create" ? createEntryMenuOpen : roomEntryMenuOpen) ? "entry-selector__menu--open" : ""}`}>
                    {(screen === "create" ? createStakeOptions : roomStakeOptions).map((stake) => {
                      const active = screen === "create"
                        ? createStake === stake
                        : stake === 0
                          ? room?.tableType !== "stake"
                          : room?.tableType === "stake" && room?.stakeChips === stake;
                      return (
                        <button
                          key={stake}
                          type="button"
                          className={`entry-selector__option ${active ? "entry-selector__option--active" : ""}`}
                          onClick={() => {
                            if (screen === "create") {
                              setCreateEntryMenuOpen(false);
                              if (active) return;
                              setCreateStake(stake);
                              setCreateTableType(stake === 0 ? "casual" : "stake");
                              return;
                            }
                            setRoomEntryMenuOpen(false);
                            if (!room || active) return;
                            void updateRoomStakeOverHttp(room.roomId, stake, "http_primary_stake");
                          }}
                        >
                          {formatStakeOptionLabel(stake)}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : (
                <div className="hero-stat hero-stat--entry">
                  <span>{heroSecondaryLabel.label}</span>
                  <strong>{heroSecondaryLabel.value}</strong>
                </div>
              )
            ) : null}
          </div>
        ) : null}
      </header>
      ) : null}

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
                disabled={createRoomBusy || !initReadyForServerActions}
                onClick={() => {
                  if (createRoomBusy) return;
                  if (!initReadyForServerActions) {
                    if (isServer) void fetchBalanceOverHttp("create_gate_retry");
                    setErrorMessage(isServer ? "aguarde as fichas carregarem para abrir a sala" : "aguarde a activity terminar de carregar");
                    return;
                  }
                  const nextStake = isServer ? 25 : 0;
                  const nextTableType: TableType = isServer ? "stake" : "casual";
                  resetGameRuntimeState(currentRoomRef.current?.roomId, { clearGame: true, reason: 'home_click_create_reset' });
                  setRoom(null);
                  setCreateDraftRoomId(null);
                  setLocallyOwnedRoomId(null);
                  setCreateTableType(nextTableType);
                  setCreateStake(nextStake);
                  setCreateEntryMenuOpen(false);
                  setRoomEntryMenuOpen(false);
                  setCreateRoomBusy(true);
                  setErrorMessage(null);
                  void (async () => {
                    try {
                      const nextRoom = await createRoomOverHttp("home_click_create", { stake: nextStake, tableType: nextTableType });
                      if (nextRoom) {
                        setRoom(nextRoom);
                        setScreen("room");
                      } else {
                        setErrorMessage("não foi possível abrir a sala agora");
                      }
                    } finally {
                      setCreateRoomBusy(false);
                    }
                  })();
                }}
              >
                <span className="menu-button__eyebrow">Mesa nova</span>
                <strong>{createRoomBusy ? "Abrindo mesa..." : "Criar mesa"}</strong>
                <small>{createRoomBusy ? "Entrando na sala..." : (!initReadyForServerActions && isServer ? "Carregando fichas..." : "Abra uma mesa.")}</small>
              </button>

              <button
                className="menu-button menu-button--join"
                type="button"
                onClick={() => {
                  setScreen("list");
                  void requestRooms();
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

            {screen === "create" ? (
        <section className="lobby-panel lobby-panel--compact lobby-panel--create">
          <div className="list-topbar list-topbar--create list-topbar--compact-create list-topbar--single">
            <button className="chip-button chip-button--back" type="button" onClick={() => {
              const roomId = createDraftRoomIdRef.current;
              if (roomId) {
                void leaveRoomOverHttp(roomId, "http_primary_close_create", { closeRoom: true });
              }
              resetGameRuntimeState(roomId, { clearGame: true, reason: 'close_create_room' });
              setCreateDraftRoomId(null);
              setLocallyOwnedRoomId(null);
              setRoom(null);
              setScreen("home");
              void requestRooms();
            }}>Fechar sala</button>
          </div>

          <div className="create-layout create-layout--final">
            <div className="create-preview-card create-preview-card--final create-preview-card--single">
              <div className="create-preview-shell create-preview-shell--final create-preview-shell--create-compact">
                <div className="participant-slot participant-slot--filled participant-slot--compact participant-slot--create-main">
                  <div className="participant-slot__avatar-wrap">
                    <img
                      className="participant-slot__avatar"
                      src={resolvePlayerAvatar(createPreviewHostPlayer ?? { userId: state.currentUser.userId, avatarUrl: state.currentUser.avatarUrl ?? null })}
                      alt={(createPreviewHostPlayer?.displayName ?? state.currentUser.displayName)}
                    />
                  </div>
                  <span className="participant-slot__name">{cleanPlayerName({ displayName: createPreviewHostPlayer?.displayName ?? state.currentUser.displayName })}</span>
                  <small className="participant-slot__role">você</small>
                </div>

                {createPreviewOpponentPlayer ? (
                  <div className="participant-slot participant-slot--filled participant-slot--compact participant-slot--create-main">
                    <div className="participant-slot__avatar-wrap">
                      <img className="participant-slot__avatar" src={resolvePlayerAvatar(createPreviewOpponentPlayer)} alt={createPreviewOpponentPlayer.displayName} />
                    </div>
                    <span className="participant-slot__name">{cleanPlayerName(createPreviewOpponentPlayer)}</span>
                    <small className="participant-slot__role">jogador</small>
                  </div>
                ) : (
                  <div className="participant-slot participant-slot--ghost participant-slot--compact participant-slot--create-main">
                    <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                      <div className="participant-slot__unknown">?</div>
                    </div>
                    <span className="participant-slot__name">Aguardando adversário</span>
                    <small className="participant-slot__role">vaga aberta</small>
                  </div>
                )}
              </div>

              <div className="create-preview-footer create-preview-footer--solo">
                {!resolvedUser ? (
                  <button className="primary-button create-submit create-submit--compact" type="button" disabled={authBusy} onClick={() => { void handleAuthorize(); }}>
                    {authBusy ? "Autorizando..." : "Autorizar conta"}
                  </button>
                ) : (
                  <button
                    className="primary-button create-submit create-submit--compact"
                    type="button"
                    disabled={!createPreviewRoom || (isServer && createStake > 0 && balanceLoaded && !canAffordSelectedStake)}
                    onClick={() => {
                      if (!createPreviewRoom) return;
                      setRoom(createPreviewRoom);
                      setScreen("room");
                    }}
                  >
                    {createPreviewRoom ? "Abrir mesa" : "Abrindo mesa..."}
                  </button>
                )}
              </div>

              {isServer && createStake > 0 && !balanceLoaded ? (
                <p className="plain-copy create-preview-note">Carregando fichas...</p>
              ) : null}
              {isServer && createStake > 0 && balanceLoaded && !canAffordSelectedStake ? (
                <p className="error-copy create-preview-note">Você não tem fichas suficientes para essa entrada.</p>
              ) : null}
            </div>
          </div>
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
                      <div className="participant-slot participant-slot--filled participant-slot--list-card">
                        <div className="participant-slot__avatar-wrap">
                          <img className="participant-slot__avatar" src={resolvePlayerAvatar(host)} alt={host.displayName} />
                        </div>
                        <span className="participant-slot__name">{cleanPlayerName(host)}</span>
                        <small className="participant-slot__role">anfitrião</small>
                      </div>

                      <div className="participant-slot__versus participant-slot__versus--list">vs.</div>

                      {opponent ? (
                        <div className="participant-slot participant-slot--filled participant-slot--list-card">
                          <div className="participant-slot__avatar-wrap">
                            <img className="participant-slot__avatar" src={resolvePlayerAvatar(opponent)} alt={opponent.displayName} />
                          </div>
                          <span className="participant-slot__name">{cleanPlayerName(opponent)}</span>
                          <small className="participant-slot__role">adversário</small>
                        </div>
                      ) : (
                        <div className="participant-slot participant-slot--ghost participant-slot--list-card">
                          <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                            <div className="participant-slot__unknown">?</div>
                          </div>
                          <span className="participant-slot__name">Aguardando jogador</span>
                          <small className="participant-slot__role">vaga aberta</small>
                        </div>
                      )}
                    </div>

                    <div className="room-entry-card__footer room-entry-card__footer--compact">
                      <div className="room-entry-card__meta room-entry-card__meta--chips">
                        <span className="room-inline-chip">{entry.players.length}/2</span>
                        <span className="room-inline-chip">{formatStakeOptionLabel(entry.tableType === "stake" ? (entry.stakeChips ?? 0) : 0)}</span>
                        <span className={`status-badge status-badge--${entry.status} room-inline-chip room-inline-chip--status`}>{formatStatus(entry)}</span>
                      </div>

                      <button
                        className="primary-button room-entry-card__join"
                        type="button"
                        disabled={authBusy || entry.players.length >= 2}
                        onClick={() => {
                          if (!resolvedUser) {
                            void handleAuthorize();
                            return;
                          }
                          void joinRoomOverHttp(entry.roomId, "http_primary_join");
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
        <section className="lobby-panel lobby-panel--compact lobby-panel--room-stage">
          <div className="list-topbar list-topbar--room-stage list-topbar--room-stage-compact">
            <button className="chip-button chip-button--back" type="button" disabled={roomExitBusy} onClick={() => { void exitCurrentRoom(isRoomHost ? "http_primary_close_room" : "http_primary_leave_room_top"); }}>{roomExitBusy ? (isRoomHost ? "Fechando..." : "Saindo...") : (isRoomHost ? "Fechar sala" : "Sair")}</button>
            <div className="room-stage__top-meta">
              <span className="room-stage__top-chip">{room.players.length}/2</span>
              <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>{roomTopStatus}</span>
            </div>
          </div>

          <div className="room-stage room-stage--final room-stage--compact room-stage--single">
            <div className="create-preview-card room-stage__preview room-stage__preview--compact">
              <div className="create-preview-shell create-preview-shell--room room-stage__players">
                <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-host">
                  <div className="participant-slot__avatar-wrap">
                    <img className="participant-slot__avatar" src={resolvePlayerAvatar(roomHostPlayer ?? room.players[0])} alt={room.hostDisplayName} />
                  </div>
                  <span className="participant-slot__name">{cleanPlayerName(roomHostPlayer ?? room.players[0])}</span>
                  <small className="participant-slot__role">anfitrião</small>
                  <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>
                    {canHostStart ? "pode iniciar" : roomOpponentPlayer ? "aguardando" : "vaga aberta"}
                  </span>
                </div>

                {roomOpponentPlayer ? (
                  <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-opponent">
                    <div className="participant-slot__avatar-wrap">
                      <img className="participant-slot__avatar" src={resolvePlayerAvatar(roomOpponentPlayer)} alt={roomOpponentPlayer.displayName} />
                    </div>
                    <span className="participant-slot__name">{cleanPlayerName(roomOpponentPlayer)}</span>
                    <small className="participant-slot__role">adversário</small>
                    <span className={`room-ready-badge ${roomOpponentPlayer.ready ? "room-ready-badge--ready" : ""}`}>
                      {roomOpponentPlayer.ready ? "pronto" : "aguardando"}
                    </span>
                  </div>
                ) : (
                  <div className="participant-slot participant-slot--ghost participant-slot--room-main participant-slot--room-opponent participant-slot--room-open">
                    <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                      <div className="participant-slot__unknown">?</div>
                    </div>
                    <span className="participant-slot__name">Aguardando adversário</span>
                    <small className="participant-slot__role">vaga aberta</small>
                    <span className="room-ready-badge">aguardando</span>
                  </div>
                )}
              </div>

              <div className="room-stage__footer room-stage__footer--tight">
                <div className={`room-stage__actions room-stage__actions--single ${isRoomHost ? "room-stage__actions--host" : "room-stage__actions--guest"}`}>
                  {isRoomHost ? (
                    <button
                      className={`primary-button room-stage__ready ${!canHostStart ? "primary-button--muted" : ""}`}
                      type="button"
                      disabled={!canHostStart || gameStartBusy}
                      onClick={async () => {
                        if (!canHostStart || gameStartBusy) return;
                        setGameStartBusy(true);
                        setErrorMessage(null);
                        try {
                          await startGameOverHttp(room.roomId, "http_primary_game_start", isRoomHost ? room.hostUserId : null);
                        } finally {
                          setGameStartBusy(false);
                        }
                      }}
                    >
                      {gameStartBusy ? "Abrindo mesa..." : "Iniciar partida"}
                    </button>
                  ) : (
                    <button
                      className={`primary-button room-stage__ready ${currentPlayer?.ready ? "primary-button--muted" : ""}`}
                      type="button"
                      onClick={() => {
                        const nextReady = !currentPlayer?.ready;
                        void setReadyOverHttp(room.roomId, nextReady, "http_primary_ready");
                      }}
                    >
                      {currentPlayer?.ready ? "Cancelar pronto" : "Marcar pronto"}
                    </button>
                  )}
                </div>
              </div>

              {authState === "needs_consent" && !resolvedUser ? <p className="plain-copy">Autorize sua conta para usar fichas, criar mesa e entrar em partida.</p> : null}
              {errorMessage && connectionState !== "offline" ? <p className="error-copy">{errorMessage}</p> : null}
            </div>
          </div>
        </section>
      ) : null}

      {screen === "game" && room ? (
        game ? (
          <GameStage
            room={room}
            game={game}
            currentUserId={state.currentUser.userId}
            shootBusy={gameShootBusy}
            exitBusy={roomExitBusy}
            opponentAim={remoteAim && remoteAim.roomId === room.roomId ? remoteAim : null}
            onAimStateChange={(aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; seq?: number; mode: AimPointerMode }) => {
              if (!room) return;
              const deliveredOverSocket = sendMessage({
                type: "sync_aim",
                payload: {
                  roomId: room.roomId,
                  userId: state.currentUser.userId,
                  visible: aim.visible,
                  angle: aim.angle,
                  cueX: aim.cueX ?? null,
                  cueY: aim.cueY ?? null,
                  power: aim.power ?? 0,
                  seq: aim.seq ?? 0,
                  mode: aim.mode,
                },
              }, { silent: true });
              if (!deliveredOverSocket) {
                scheduleAimHttpSync(room.roomId, aim);
              }
            }}
            onExit={() => { void exitCurrentRoom(isRoomHost ? "http_primary_close_room_game" : "http_primary_leave_room_game"); }}
            onShoot={async (shot: { angle: number; power: number; cueX?: number | null; cueY?: number | null; calledPocket?: number | null; spinX?: number | null; spinY?: number | null }) => {
              if (!room) return;
              const existingDispatch = shotDispatchRef.current;
              if (gameShootBusyRef.current || (existingDispatch.roomId === room.roomId && performance.now() - existingDispatch.startedAt < 1500)) {
                console.warn("[sinuca-shoot-ui]", JSON.stringify({ roomId: room.roomId, reason: "shot_dispatch_locked", transport: existingDispatch.transport }));
                return;
              }
              setGameShootBusy(true);
              setErrorMessage(null);
              try {
                const previousSeq = game?.roomId === room.roomId ? game.shotSequence : 0;
                armRealtimeHttpLock(room.roomId, previousSeq + 1, 'local_shot');
                const sentOverSocket = sendMessage({
                  type: "take_shot",
                  payload: {
                    roomId: room.roomId,
                    userId: state.currentUser.userId,
                    angle: shot.angle,
                    power: shot.power,
                    cueX: shot.cueX ?? null,
                    cueY: shot.cueY ?? null,
                    calledPocket: shot.calledPocket ?? null,
                    spinX: shot.spinX ?? 0,
                    spinY: shot.spinY ?? 0,
                  },
                }, { silent: true });

                if (sentOverSocket) {
                  markShotDispatch(room.roomId, previousSeq + 1, 'ws', 'ws_primary_shoot');
                  window.setTimeout(() => {
                    const latestGame = currentGameRef.current;
                    const wsState = wsGameStateRef.current;
                    const waitingForNewShot = !latestGame
                      || latestGame.roomId !== room.roomId
                      || latestGame.shotSequence <= previousSeq;
                    const lastAuthoritativeAt = wsState.roomId === room.roomId ? wsState.lastReceivedAt : 0;
                    const stalledSimulating = latestGame?.roomId === room.roomId
                      && latestGame.status === 'simulating'
                      && performance.now() - Math.max(simRecoveryRef.current.lastProgressAt, lastAuthoritativeAt) > 950;
                    if (!waitingForNewShot && !stalledSimulating) return;
                    const recovery = simRecoveryRef.current;
                    if (stalledSimulating && !recovery.inFlight) {
                      void fetchGameStateOverHttp(room.roomId, `force_recover_after_shot_${previousSeq}`, latestGame?.shotSequence ?? previousSeq);
                    } else if (waitingForNewShot && !isSocketOpen()) {
                      void fetchGameStateOverHttp(room.roomId, 'ws_verify_after_shot', previousSeq);
                    }
                  }, 900);
                  return;
                }

                const applied = await shootGameOverHttp(room.roomId, shot, "http_primary_shot_post");
                if (!applied) {
                  console.warn("[sinuca-shoot-ui]", JSON.stringify({ roomId: room.roomId, reason: "no_game_returned" }));
                }
              } finally {
                window.setTimeout(() => setGameShootBusy(false), 120);
              }
            }}
          />
        ) : (
          <section className="lobby-panel lobby-panel--compact lobby-panel--room-stage">
            <div className="empty-card empty-card--soft empty-card--home empty-card--list">
              <strong>Carregando a mesa...</strong>
              <span>Sincronizando a partida para os dois lados.</span>
              {gameLoadingTimedOut ? (
                <p className="plain-copy" style={{ marginTop: 10 }}>
                  A mesa demorou demais para sincronizar. Você pode voltar ao lobby e encerrar esta sala agora.
                </p>
              ) : null}
              <div style={{ display: "flex", justifyContent: "center", marginTop: 14 }}>
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => { void forceReturnToLobbyFromLoading(isRoomHost ? "http_force_close_loading_lobby" : "http_force_leave_loading_lobby"); }}
                  disabled={roomExitBusy}
                >
                  {roomExitBusy ? "Voltando..." : "Voltar ao lobby"}
                </button>
              </div>
            </div>
          </section>
        )
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
