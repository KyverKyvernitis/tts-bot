import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import type { PendingAimHttpState } from "../game/teardown";
import { fetchAimStateRequest, syncAimStateRequest } from "../transport/aimApi";
import type { AimPointerMode, AimStateSnapshot, GameSnapshot } from "../types/activity";

export type LocalAimStateUpdate = {
  visible: boolean;
  angle: number;
  cueX?: number | null;
  cueY?: number | null;
  power?: number | null;
  seq?: number;
  mode: AimPointerMode;
};

type UseRemoteAimControllerParams = {
  screen: string;
  roomId: string | null;
  game: Pick<GameSnapshot, "roomId" | "status" | "turnUserId"> | null;
  currentUserId: string;
  connectionState: string;
  setAuthDebug: Dispatch<SetStateAction<string | null>>;
  isRealtimeSocketHealthy: (roomId: string) => boolean;
  shouldBlockHttpAuxDuringRealtime: (roomId: string, kind: "room" | "aim", reason: string) => boolean;
};

type ScheduleAimSyncOptions = {
  allowWhileRealtimeHealthy?: boolean;
  reason?: string;
};

function appendAuthDebug(setAuthDebug: Dispatch<SetStateAction<string | null>>, message: string) {
  setAuthDebug((current) => current ? `${current} • ${message}` : message);
}

function clearRemoteAimState(setRemoteAim: Dispatch<SetStateAction<AimStateSnapshot | null>>) {
  setRemoteAim((current) => current ? null : current);
}

function shouldKeepFetchedAim(aim: AimStateSnapshot) {
  const shouldKeepRemoteCuePlacement = aim.cueX !== null && aim.cueY !== null && aim.mode !== "idle";
  return (aim.visible && aim.mode !== "idle") || shouldKeepRemoteCuePlacement;
}

export function useRemoteAimController({
  screen,
  roomId,
  game,
  currentUserId,
  connectionState,
  setAuthDebug,
  isRealtimeSocketHealthy,
  shouldBlockHttpAuxDuringRealtime,
}: UseRemoteAimControllerParams) {
  const [remoteAim, setRemoteAim] = useState<AimStateSnapshot | null>(null);
  const pendingAimHttpRef = useRef<PendingAimHttpState>(null);
  const aimHttpTimerRef = useRef<number | null>(null);
  const lastAimHttpSentAtRef = useRef(0);
  const lastRemoteAimAtRef = useRef<Record<string, number>>({});

  const acceptIncomingAim = (payload: AimStateSnapshot | null) => {
    if (!payload || payload.userId === currentUserId) return;
    lastRemoteAimAtRef.current[payload.roomId] = payload.updatedAt;
    setRemoteAim((current) => {
      if (!current || current.roomId !== payload.roomId || current.userId !== payload.userId) {
        return payload;
      }
      const currentRevision = Number.isFinite(current.snapshotRevision) ? current.snapshotRevision : 0;
      const nextRevision = Number.isFinite(payload.snapshotRevision) ? payload.snapshotRevision : 0;
      if (nextRevision < currentRevision) return current;
      if (nextRevision === currentRevision) {
        if (payload.seq < current.seq) return current;
        if (payload.seq === current.seq && payload.updatedAt < current.updatedAt) return current;
      }
      return payload;
    });
  };

  const fetchAimStateOverHttp = async (nextRoomId: string, reason: string, options?: { allowWhileRealtimeHealthy?: boolean }) => {
    if (!options?.allowWhileRealtimeHealthy && shouldBlockHttpAuxDuringRealtime(nextRoomId, "aim", reason)) {
      return null;
    }
    const result = await fetchAimStateRequest(nextRoomId);
    if (result.data?.aim) {
      return result.data.aim;
    }
    if (result.attempts.length) {
      appendAuthDebug(setAuthDebug, `aim_get_http_failed:${reason}:${nextRoomId}:${result.attempts.join(" | ")}`);
    }
    return null;
  };

  const scheduleAimHttpSync = (
    nextRoomId: string,
    aim: LocalAimStateUpdate,
    options?: ScheduleAimSyncOptions,
  ) => {
    const allowWhileRealtimeHealthy = Boolean(options?.allowWhileRealtimeHealthy);
    if (!allowWhileRealtimeHealthy && isRealtimeSocketHealthy(nextRoomId)) return;
    const requestReason = options?.reason ?? (allowWhileRealtimeHealthy ? "ws_backup" : "room_aim_sync");
    if (!allowWhileRealtimeHealthy && shouldBlockHttpAuxDuringRealtime(nextRoomId, "aim", requestReason)) return;
    pendingAimHttpRef.current = { roomId: nextRoomId, aim };
    const flush = () => {
      const pending = pendingAimHttpRef.current;
      if (!pending) return;
      pendingAimHttpRef.current = null;
      lastAimHttpSentAtRef.current = Date.now();
      void syncAimStateRequest({
        roomId: pending.roomId,
        userId: currentUserId,
        visible: pending.aim.visible,
        angle: pending.aim.angle,
        cueX: pending.aim.cueX ?? null,
        cueY: pending.aim.cueY ?? null,
        power: pending.aim.power ?? 0,
        seq: pending.aim.seq ?? 0,
        mode: pending.aim.mode,
      }).then((result) => {
        if (!result.data && result.attempts.length) {
          appendAuthDebug(setAuthDebug, `aim_sync_http_failed:${requestReason}:${pending.roomId}:${result.attempts.join(" | ")}`);
        }
      }).catch(() => {});
    };
    const now = Date.now();
    const minGap = aim.mode === "place" ? 24 : aim.mode === "power" ? 32 : 40;
    const wait = Math.max(0, minGap - (now - lastAimHttpSentAtRef.current));
    if (aimHttpTimerRef.current !== null) window.clearTimeout(aimHttpTimerRef.current);
    if (wait === 0 || !aim.visible || aim.mode === "idle") {
      flush();
      return;
    }
    aimHttpTimerRef.current = window.setTimeout(() => {
      aimHttpTimerRef.current = null;
      flush();
    }, wait);
  };

  const pruneRemoteAimForGameState = (nextGame: Pick<GameSnapshot, "roomId" | "status" | "turnUserId">) => {
    setRemoteAim((current) => current?.roomId === nextGame.roomId && nextGame.turnUserId !== currentUserId && nextGame.status === "waiting_shot" ? current : null);
  };

  useEffect(() => {
    if (screen !== "game" || !roomId || !game) {
      clearRemoteAimState(setRemoteAim);
      return;
    }
    if (game.turnUserId === currentUserId || game.status !== "waiting_shot") {
      setRemoteAim(null);
    }
  }, [currentUserId, game?.roomId, game?.status, game?.turnUserId, roomId, screen]);

  useEffect(() => {
    if (screen !== "game" || !roomId || !game) {
      return;
    }
    if (game.status === "finished" || game.status === "simulating" || game.turnUserId === currentUserId) {
      return;
    }
    let cancelled = false;
    let inFlight = false;
    const pollIntervalMs = connectionState === "connected" ? 95 : 60;
    const freshnessMs = connectionState === "connected" ? 150 : 220;

    const applyFetchedAim = (aim: AimStateSnapshot | null) => {
      if (cancelled) return;
      if (!aim || aim.roomId !== roomId || aim.userId === currentUserId) {
        return;
      }
      if (shouldKeepFetchedAim(aim)) {
        acceptIncomingAim(aim);
      } else {
        setRemoteAim((current) => current?.roomId === roomId ? null : current);
      }
    };

    const tick = async () => {
      if (cancelled || inFlight) return;
      const currentRemoteAim = remoteAim && remoteAim.roomId === roomId ? remoteAim : null;
      const lastSeenAt = Math.max(
        lastRemoteAimAtRef.current[roomId] ?? 0,
        currentRemoteAim?.updatedAt ?? 0,
      );
      const needsReconcile = !currentRemoteAim || Date.now() - lastSeenAt >= freshnessMs;
      if (!needsReconcile && connectionState === "connected") return;
      inFlight = true;
      try {
        const aim = await fetchAimStateOverHttp(roomId, connectionState === "connected" ? "reconcile_waiting_turn_loop" : "poll_loop", {
          allowWhileRealtimeHealthy: connectionState === "connected",
        });
        applyFetchedAim(aim);
      } finally {
        inFlight = false;
      }
    };

    void tick();
    const interval = window.setInterval(() => { void tick(); }, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [connectionState, currentUserId, game?.roomId, game?.status, game?.turnUserId, remoteAim, roomId, screen]);

  useEffect(() => () => {
    if (aimHttpTimerRef.current !== null) {
      window.clearTimeout(aimHttpTimerRef.current);
      aimHttpTimerRef.current = null;
    }
  }, []);

  return {
    remoteAim,
    acceptIncomingAim,
    pruneRemoteAimForGameState,
    scheduleAimHttpSync,
    resetDeps: {
      aimHttpTimerRef,
      pendingAimHttpRef,
      lastAimHttpSentAtRef,
      setRemoteAim,
    },
  };
}
