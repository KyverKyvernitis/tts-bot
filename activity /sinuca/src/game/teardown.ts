import type { AimPointerMode, AimStateSnapshot, GameSnapshot } from "../types/activity";
import type { GameBootstrapSessionState, RealtimeHttpLockState, SimRecoveryState, SnapshotDebugState, WsGameStateRefState } from "./bootstrap";

export type ShotDispatchState = {
  roomId: string | null;
  expectedShotSequence: number;
  transport: "ws" | "http" | null;
  startedAt: number;
  reason: string | null;
};

export type PendingAimHttpState = {
  roomId: string;
  aim: {
    visible: boolean;
    angle: number;
    cueX?: number | null;
    cueY?: number | null;
    power?: number | null;
    seq?: number;
    mode: AimPointerMode;
  };
} | null;

export type ResetGameRuntimeOptions = {
  clearGame?: boolean;
  reason?: string;
};

export type ResetGameRuntimeDeps = {
  currentRoomId: string | null;
  currentGameRoomId: string | null;
  clearShotDispatch: (roomId?: string | null, reason?: string) => void;
  clearRealtimeHttpLock: (roomId?: string | null) => void;
  wsGameStateRef: { current: WsGameStateRefState };
  simRecoveryRef: { current: SimRecoveryState };
  snapshotDebugRef: { current: SnapshotDebugState };
  aimHttpTimerRef: { current: number | null };
  pendingAimHttpRef: { current: PendingAimHttpState };
  lastAimHttpSentAtRef: { current: number };
  setRemoteAim: (value: AimStateSnapshot | null) => void;
  setGameShootBusy: (value: boolean) => void;
  setGame: (value: GameSnapshot | null) => void;
  currentGameRef?: { current: GameSnapshot | null };
  gameBootstrapSessionRef?: { current: GameBootstrapSessionState };
  clearTimeoutFn?: (timeoutId: number) => void;
};

export function resetGameRuntimeState(
  deps: ResetGameRuntimeDeps,
  roomId?: string | null,
  options?: ResetGameRuntimeOptions,
) {
  const targetRoomId = roomId ?? deps.currentRoomId ?? deps.currentGameRoomId ?? null;
  deps.clearShotDispatch(targetRoomId, options?.reason ?? "runtime_reset");
  deps.clearRealtimeHttpLock(targetRoomId);
  deps.wsGameStateRef.current = { roomId: null, lastReceivedAt: 0, shotSequence: 0, revision: 0 };
  deps.simRecoveryRef.current = {
    roomId: null,
    shotSequence: 0,
    revision: 0,
    lastProgressAt: 0,
    lastRecoveryAt: 0,
    recoveryCount: 0,
    inFlight: false,
    lastRequestedShotSequence: 0,
    lastRequestedRevision: 0,
  };
  deps.snapshotDebugRef.current = {
    roomId: null,
    lastReceivedAt: 0,
    lastLoggedAt: 0,
    lastRevision: -1,
    lastSource: null,
  };
  if (deps.aimHttpTimerRef.current !== null) {
    (deps.clearTimeoutFn ?? window.clearTimeout)(deps.aimHttpTimerRef.current);
    deps.aimHttpTimerRef.current = null;
  }
  deps.pendingAimHttpRef.current = null;
  deps.lastAimHttpSentAtRef.current = 0;
  if (deps.gameBootstrapSessionRef) {
    deps.gameBootstrapSessionRef.current.token += 1;
    deps.gameBootstrapSessionRef.current.roomId = null;
    deps.gameBootstrapSessionRef.current.expectedGameId = null;
    deps.gameBootstrapSessionRef.current.startedAt = 0;
    deps.gameBootstrapSessionRef.current.completedAt = 0;
  }
  deps.setRemoteAim(null);
  deps.setGameShootBusy(false);
  if (options?.clearGame ?? true) {
    if (deps.currentGameRef) deps.currentGameRef.current = null;
    deps.setGame(null);
  }
}
