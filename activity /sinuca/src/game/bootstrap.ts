import type { GameSnapshot } from "../types/activity";

export type WsGameStateRefState = {
  roomId: string | null;
  lastReceivedAt: number;
  shotSequence: number;
  revision: number;
};

export type RealtimeHttpLockState = {
  roomId: string | null;
  shotSequence: number;
  armedAt: number;
  source: string | null;
};

export type SimRecoveryState = {
  roomId: string | null;
  shotSequence: number;
  revision: number;
  lastProgressAt: number;
  lastRecoveryAt: number;
  recoveryCount: number;
  inFlight: boolean;
  lastRequestedShotSequence: number;
  lastRequestedRevision: number;
};

export type SnapshotDebugState = {
  roomId: string | null;
  lastReceivedAt: number;
  lastLoggedAt: number;
  lastRevision: number;
  lastSource: string | null;
};

export const GAME_SIM_WATCHDOG_INTERVAL_MS = 220;
export const GAME_SIM_STALL_RECOVERY_MS = 900;
export const GAME_BOOTSTRAP_RETRY_INTERVAL_MS = 350;
export const GAME_POLL_INTERVAL_MS = 220;
export const GAME_LOADING_TIMEOUT_MS = 8000;
export const REALTIME_GAME_HEALTH_MAX_AGE_MS = 1800;

export function isRealtimeSocketHealthy(params: {
  isSocketOpen: boolean;
  roomId?: string | null;
  wsState: WsGameStateRefState;
}) {
  const { isSocketOpen, roomId, wsState } = params;
  if (!isSocketOpen) return false;
  if (!roomId) return true;
  if (wsState.roomId !== roomId) return false;
  if (!wsState.lastReceivedAt) return false;
  return performance.now() - wsState.lastReceivedAt <= REALTIME_GAME_HEALTH_MAX_AGE_MS;
}

export function getRealtimeHttpGuardState(params: {
  roomId: string;
  activeGame: GameSnapshot | null;
  activeRoomId: string | null;
  lock: RealtimeHttpLockState;
}) {
  const { roomId, activeGame, activeRoomId, lock } = params;
  const sameRoom = activeGame?.roomId === roomId || activeRoomId === roomId || lock.roomId === roomId;
  if (!sameRoom) {
    return {
      sameRoom: false,
      isRealtimeLocked: false,
      activeGame,
      lock,
      activeGameSimulating: false,
      localLockActive: false,
    };
  }

  const activeGameSimulating = activeGame?.roomId === roomId && activeGame.status === "simulating";
  const localLockActive = lock.roomId === roomId;
  return {
    sameRoom: true,
    isRealtimeLocked: activeGameSimulating || localLockActive,
    activeGame,
    lock,
    activeGameSimulating,
    localLockActive,
  };
}

export function shouldBlockHttpGameDuringRealtime(params: {
  roomId: string;
  reason: string;
  activeGame: GameSnapshot | null;
  activeRoomId: string | null;
  lock: RealtimeHttpLockState;
}) {
  const { roomId, reason, activeGame, activeRoomId, lock } = params;
  const guard = getRealtimeHttpGuardState({ roomId, activeGame, activeRoomId, lock });
  if (!guard.sameRoom || !guard.isRealtimeLocked) return false;
  if (reason.startsWith("force_recover_")) return false;
  return true;
}

export function shouldRunHttpGamePolling(params: {
  roomId: string;
  activeGame: GameSnapshot | null;
  activeRoomId: string | null;
  lock: RealtimeHttpLockState;
  isRealtimeHealthy: boolean;
}) {
  const { roomId, activeGame, activeRoomId, lock, isRealtimeHealthy } = params;
  const guard = getRealtimeHttpGuardState({ roomId, activeGame, activeRoomId, lock });
  if (guard.isRealtimeLocked) return false;
  if (!activeGame || activeGame.roomId !== roomId) return true;
  if (isRealtimeHealthy) return false;
  return true;
}

export function needsGameBootstrap(roomId: string, activeGame: GameSnapshot | null) {
  return activeGame?.roomId !== roomId;
}
