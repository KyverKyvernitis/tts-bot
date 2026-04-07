import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import {
  GAME_BOOTSTRAP_RETRY_INTERVAL_MS,
  GAME_LOADING_TIMEOUT_MS,
  GAME_POLL_INTERVAL_MS,
  GAME_SIM_STALL_RECOVERY_MS,
  GAME_SIM_WATCHDOG_INTERVAL_MS,
  ensureGameBootstrapSession,
  needsGameBootstrap,
  type GameBootstrapSessionState,
  type SimRecoveryState,
  type WsGameStateRefState,
} from "../game/bootstrap";
import type { GameSnapshot, RoomSnapshot } from "../types/activity";

export type LobbyScreen = "home" | "create" | "list" | "room" | "game";

export type UseGameControllerParams = {
  bootstrapped: boolean;
  screen: LobbyScreen;
  room: RoomSnapshot | null;
  game: GameSnapshot | null;
  roomExitBusy: boolean;
  isRoomHost: boolean;
  currentUserId: string;
  currentGameRef: { current: GameSnapshot | null };
  currentRoomRef: { current: RoomSnapshot | null };
  currentScreenRef: { current: LobbyScreen };
  locallyOwnedRoomIdRef: { current: string | null };
  unloadLeaveSentRef: { current: string | null };
  simRecoveryRef: { current: SimRecoveryState };
  wsGameStateRef: { current: WsGameStateRefState };
  gameBootstrapSessionRef: { current: GameBootstrapSessionState };
  setRoomExitBusy: Dispatch<SetStateAction<boolean>>;
  setErrorMessage: Dispatch<SetStateAction<string | null>>;
  setRoomEntryMenuOpen: Dispatch<SetStateAction<boolean>>;
  setCreateEntryMenuOpen: Dispatch<SetStateAction<boolean>>;
  setCreateDraftRoomId: Dispatch<SetStateAction<string | null>>;
  setLocallyOwnedRoomId: Dispatch<SetStateAction<string | null>>;
  setRoom: Dispatch<SetStateAction<RoomSnapshot | null>>;
  setGame: Dispatch<SetStateAction<GameSnapshot | null>>;
  setGameStartBusy: Dispatch<SetStateAction<boolean>>;
  setScreen: Dispatch<SetStateAction<LobbyScreen>>;
  requestRooms: () => Promise<void>;
  leaveRoomOverHttp: (roomId: string, reason: string, options?: { closeRoom?: boolean }) => Promise<{ room?: RoomSnapshot | null; closed?: boolean; error?: string; detail?: string } | null>;
  dispatchLeaveBeacon: (roomId: string, userId: string, closeRoom: boolean) => boolean;
  resetGameRuntimeState: (roomId?: string | null, options?: { clearGame?: boolean; reason?: string }) => void;
  logSnapshotDebug: (scope: string, payload: Record<string, unknown>) => void;
  isSocketOpen: () => boolean;
  shouldRunHttpGamePolling: (roomId: string) => boolean;
  fetchGameStateOverHttp: (roomId: string, reason: string, sinceSeq?: number, bootstrapToken?: number) => Promise<GameSnapshot | null>;
};

export function useGameController(params: UseGameControllerParams) {
  const {
    bootstrapped,
    screen,
    room,
    game,
    roomExitBusy,
    isRoomHost,
    currentUserId,
    currentGameRef,
    currentRoomRef,
    currentScreenRef,
    locallyOwnedRoomIdRef,
    unloadLeaveSentRef,
    simRecoveryRef,
    wsGameStateRef,
    gameBootstrapSessionRef,
    setRoomExitBusy,
    setErrorMessage,
    setRoomEntryMenuOpen,
    setCreateEntryMenuOpen,
    setCreateDraftRoomId,
    setLocallyOwnedRoomId,
    setRoom,
    setGame,
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
  } = params;

  const [gameLoadingTimedOut, setGameLoadingTimedOut] = useState(false);
  const [loadingOverlayDebug, setLoadingOverlayDebug] = useState("");

  useEffect(() => {
    if (!bootstrapped || screen !== 'game' || !room?.roomId) return;

    const interval = window.setInterval(() => {
      const activeGame = currentGameRef.current;
      if (!activeGame || activeGame.roomId !== room.roomId) return;
      if (activeGame.status !== 'simulating') return;

      const now = performance.now();
      const recovery = simRecoveryRef.current;
      const wsState = wsGameStateRef.current;
      const lastAuthoritativeAt = wsState.roomId === activeGame.roomId ? wsState.lastReceivedAt : 0;
      const lastProgressAt = Math.max(recovery.lastProgressAt, lastAuthoritativeAt);
      const stalledForMs = lastProgressAt > 0 ? now - lastProgressAt : Number.POSITIVE_INFINITY;
      if (stalledForMs < GAME_SIM_STALL_RECOVERY_MS) return;
      if (recovery.inFlight) return;

      const cooldownMs = recovery.recoveryCount > 0 ? 1400 : 1000;
      if (now - recovery.lastRecoveryAt < cooldownMs) return;

      recovery.recoveryCount += 1;
      logSnapshotDebug('recover', {
        source: 'http',
        roomId: activeGame.roomId,
        reason: 'force_recover_watchdog',
        status: activeGame.status,
        shotSequence: activeGame.shotSequence,
        revision: Number.isFinite(activeGame.snapshotRevision) ? activeGame.snapshotRevision : 0,
        stalledForMs: Math.round(stalledForMs),
        wsOpen: isSocketOpen(),
        recoveryCount: recovery.recoveryCount,
      });
      void fetchGameStateOverHttp(activeGame.roomId, `force_recover_watchdog_${activeGame.shotSequence}`, activeGame.shotSequence);
    }, GAME_SIM_WATCHDOG_INTERVAL_MS);

    return () => window.clearInterval(interval);
  }, [bootstrapped, currentGameRef, fetchGameStateOverHttp, isSocketOpen, logSnapshotDebug, room?.roomId, screen, simRecoveryRef, wsGameStateRef]);

  useEffect(() => {
    if (!bootstrapped || screen !== 'game' || !room?.roomId) return;
    const roomId = room.roomId;

    const bootstrapToken = ensureGameBootstrapSession(gameBootstrapSessionRef.current, roomId, null);
    const needsBootstrapForRoom = () => needsGameBootstrap(roomId, currentGameRef.current, gameBootstrapSessionRef.current);

    if (needsBootstrapForRoom()) {
      logSnapshotDebug('recover', {
        source: 'http',
        roomId,
        reason: 'force_bootstrap_missing',
        status: currentGameRef.current?.status ?? null,
        shotSequence: currentGameRef.current?.shotSequence ?? null,
        revision: Number.isFinite(currentGameRef.current?.snapshotRevision) ? currentGameRef.current!.snapshotRevision : null,
        why: 'screen_game_without_snapshot',
        wsOpen: isSocketOpen(),
        wsRoomId: wsGameStateRef.current.roomId,
        wsAgeMs: wsGameStateRef.current.lastReceivedAt ? Math.round(performance.now() - wsGameStateRef.current.lastReceivedAt) : null,
      });
      void fetchGameStateOverHttp(roomId, 'force_bootstrap_missing', 0, bootstrapToken);
    }

    const interval = window.setInterval(() => {
      if (!needsBootstrapForRoom()) return;
      const retryToken = ensureGameBootstrapSession(gameBootstrapSessionRef.current, roomId, null);
      void fetchGameStateOverHttp(roomId, 'force_bootstrap_retry', 0, retryToken);
    }, GAME_BOOTSTRAP_RETRY_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [bootstrapped, currentGameRef, fetchGameStateOverHttp, gameBootstrapSessionRef, isSocketOpen, logSnapshotDebug, room?.roomId, screen, wsGameStateRef]);

  useEffect(() => {
    if (!bootstrapped || screen !== 'game' || !room?.roomId) return;
    if (!shouldRunHttpGamePolling(room.roomId)) {
      logSnapshotDebug('skip', {
        source: 'http',
        roomId: room.roomId,
        reason: 'game_poll_effect_guard',
        status: currentGameRef.current?.status ?? null,
        shotSequence: currentGameRef.current?.shotSequence ?? null,
        revision: Number.isFinite(currentGameRef.current?.snapshotRevision) ? currentGameRef.current!.snapshotRevision : null,
        why: isSocketOpen() ? 'ws_open' : 'simulating_guard',
      });
      return;
    }
    void fetchGameStateOverHttp(room.roomId, 'game_initial', game?.shotSequence ?? 0);
    const interval = window.setInterval(() => {
      if (!shouldRunHttpGamePolling(room.roomId)) return;
      void fetchGameStateOverHttp(room.roomId, 'game_poll', game?.shotSequence ?? 0);
    }, GAME_POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [bootstrapped, currentGameRef, fetchGameStateOverHttp, game?.shotSequence, isSocketOpen, logSnapshotDebug, room?.roomId, screen, shouldRunHttpGamePolling]);

  useEffect(() => {
    if (screen !== 'game' || !room?.roomId || game) {
      setGameLoadingTimedOut(false);
      return;
    }
    setGameLoadingTimedOut(false);
    const timeout = window.setTimeout(() => {
      setGameLoadingTimedOut(true);
    }, GAME_LOADING_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [game, room?.roomId, screen]);

  useEffect(() => {
    if (screen !== 'game' || !room?.roomId) {
      setLoadingOverlayDebug("");
      return;
    }

    const currentRefGame = currentGameRef.current;
    const bootstrap = gameBootstrapSessionRef.current;
    const wsState = wsGameStateRef.current;
    const hasStateGame = Boolean(game && game.roomId === room.roomId);
    const hasRefGame = Boolean(currentRefGame && currentRefGame.roomId === room.roomId);

    if (!hasStateGame && hasRefGame && currentRefGame) {
      const sessionMatches = bootstrap.roomId === room.roomId
        && (!bootstrap.expectedGameId || bootstrap.expectedGameId === currentRefGame.gameId);
      if (sessionMatches) {
        logSnapshotDebug('recover', {
          source: 'local',
          roomId: room.roomId,
          gameId: currentRefGame.gameId,
          reason: 'hydrate_state_from_current_game_ref',
          bootstrapToken: bootstrap.token,
          bootstrapGameId: bootstrap.expectedGameId,
        });
        setGame(currentRefGame);
        setGameLoadingTimedOut(false);
      }
    }

    const phase = hasStateGame ? 'ready_state_game' : hasRefGame ? 'ref_game_only' : bootstrap.completedAt ? 'awaiting_state_after_complete' : 'bootstrapping';
    const wsAgeMs = wsState.lastReceivedAt ? Math.round(performance.now() - wsState.lastReceivedAt) : null;
    setLoadingOverlayDebug([
      `phase=${phase}`,
      `roomId=${room.roomId}`,
      `gameState=${hasStateGame ? (game?.gameId ?? 'present') : 'null'}`,
      `refGame=${hasRefGame ? (currentRefGame?.gameId ?? 'present') : 'null'}`,
      `expectedGameId=${bootstrap.expectedGameId ?? 'null'}`,
      `bootstrapToken=${bootstrap.token}`,
      `bootstrapDone=${bootstrap.completedAt ? 'yes' : 'no'}`,
      `wsRoomId=${wsState.roomId ?? 'null'}`,
      `wsAgeMs=${wsAgeMs ?? 'null'}`,
      `loadingTimedOut=${gameLoadingTimedOut ? 'yes' : 'no'}`,
    ].join('\n'));
  }, [
    currentGameRef,
    game,
    gameBootstrapSessionRef,
    gameLoadingTimedOut,
    logSnapshotDebug,
    room?.roomId,
    screen,
    setGame,
    wsGameStateRef,
  ]);

  const forceReturnToLobbyFromLoading = useCallback(async (reason: string) => {
    if (!room || game || roomExitBusy) return;
    const roomId = room.roomId;
    const closeRoom = room.hostUserId === currentUserId || locallyOwnedRoomIdRef.current === roomId;
    const nextScreen: LobbyScreen = closeRoom ? 'home' : 'list';

    setRoomExitBusy(true);
    setErrorMessage(null);

    try {
      const result = await leaveRoomOverHttp(roomId, reason, { closeRoom });
      if (result === null) {
        dispatchLeaveBeacon(roomId, currentUserId, closeRoom);
        setErrorMessage(closeRoom
          ? 'A mesa travou no carregamento. Voltando ao lobby e tentando fechar a sala.'
          : 'A mesa travou no carregamento. Voltando ao lobby e tentando sair da sala.');
      }
    } finally {
      unloadLeaveSentRef.current = null;
      setRoomEntryMenuOpen(false);
      setCreateEntryMenuOpen(false);
      setCreateDraftRoomId(null);
      setLocallyOwnedRoomId((current) => current === roomId ? null : current);
      resetGameRuntimeState(roomId, { clearGame: true, reason });
      setRoom(null);
      setGameStartBusy(false);
      setScreen(nextScreen);
      setRoomExitBusy(false);
      void requestRooms();
    }
  }, [
    currentUserId,
    dispatchLeaveBeacon,
    game,
    leaveRoomOverHttp,
    locallyOwnedRoomIdRef,
    requestRooms,
    resetGameRuntimeState,
    room,
    roomExitBusy,
    setCreateDraftRoomId,
    setCreateEntryMenuOpen,
    setErrorMessage,
    setGameStartBusy,
    setLocallyOwnedRoomId,
    setRoom,
    setRoomEntryMenuOpen,
    setRoomExitBusy,
    setScreen,
    unloadLeaveSentRef,
  ]);

  const syncGameScreenState = useCallback((nextScreen: LobbyScreen) => {
    currentScreenRef.current = nextScreen;
    setScreen(nextScreen);
  }, [currentScreenRef, setScreen]);

  return {
    gameLoadingTimedOut,
    loadingOverlayDebug,
    forceReturnToLobbyFromLoading,
    syncGameScreenState,
    resetGameLoadingTimeout: () => setGameLoadingTimedOut(false),
    markGameScreenLoading: () => setGameLoadingTimedOut(false),
  };
}
