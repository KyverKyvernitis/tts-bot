import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type TouchEvent as ReactTouchEvent } from "react";
import type { AimPointerMode, AimStateSnapshot, BallGroup, GameBallSnapshot, GameShotFrame, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
import tableAsset from "../assets/game/pool-table-public.png";
import cueAsset from "../assets/game/pool-cue-public.png";
import type { ShotPipelineDebugEvent } from "../screens/GameScreen";

// ─── Ball sprite imports removed — we use canvas-rendered balls exclusively ────
// The canvas drawFallbackBall() renders much higher quality balls with
// 3D gradients, numbers, stripes, specular highlights than the old PNG sprites.

// ─── Web Audio API sound engine ────────────────────────────────────────────
const SFX = (() => {
  let ctx: AudioContext | null = null;
  const getCtx = () => {
    if (!ctx) { try { ctx = new AudioContext(); } catch { return null; } }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  };

  function noise(ac: AudioContext, duration: number, volume: number, freq: number, decay: number) {
    const len = Math.ceil(ac.sampleRate * duration);
    const buf = ac.createBuffer(1, len, ac.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) {
      const t = i / ac.sampleRate;
      const env = Math.exp(-t * decay);
      const osc = Math.sin(2 * Math.PI * freq * t);
      const nz = (Math.random() * 2 - 1) * 0.3;
      data[i] = (osc * 0.7 + nz) * env * volume;
    }
    return buf;
  }

  function play(buffer: AudioBuffer, ac: AudioContext, vol = 0.5) {
    const src = ac.createBufferSource();
    const gain = ac.createGain();
    gain.gain.value = Math.min(1, Math.max(0, vol));
    src.buffer = buffer;
    src.connect(gain).connect(ac.destination);
    src.start();
  }

  return {
    prime() {
      getCtx();
    },
    /** Cue hitting the ball — sharp high click */
    cueHit(power = 0.7) {
      const ac = getCtx(); if (!ac) return;
      const vol = 0.25 + power * 0.35;
      play(noise(ac, 0.08, vol, 1800 + power * 600, 45), ac, vol);
    },
    /** Ball falling into pocket — deep satisfying thud */
    pocket() {
      const ac = getCtx(); if (!ac) return;
      play(noise(ac, 0.18, 0.4, 180, 14), ac, 0.5);
    },
    /** Cushion bounce — soft bump */
    cushion() {
      const ac = getCtx(); if (!ac) return;
      play(noise(ac, 0.06, 0.15, 400, 55), ac, 0.2);
    },
    /** Ball-ball collision — mid click */
    ballHit() {
      const ac = getCtx(); if (!ac) return;
      play(noise(ac, 0.05, 0.2, 1200, 60), ac, 0.25);
    },
  };
})();

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 13;
const BALL_VISUAL_RADIUS = 16;
const BALL_DIAMETER = BALL_RADIUS * 2;
const MAX_PLAYBACK_DURATION_MS = 3500;
const FELT_LEFT = 69;
const FELT_TOP = 50;
const FELT_RIGHT = TABLE_WIDTH - FELT_LEFT;
const FELT_BOTTOM = TABLE_HEIGHT - FELT_TOP;
const PLAY_MIN_X = FELT_LEFT + BALL_RADIUS;
const PLAY_MAX_X = FELT_RIGHT - BALL_RADIUS;
const PLAY_MIN_Y = FELT_TOP + BALL_RADIUS;
const PLAY_MAX_Y = FELT_BOTTOM - BALL_RADIUS;
const HEAD_STRING_X = 328;
const BREAK_MAX_X = HEAD_STRING_X - BALL_RADIUS - 6;
const DEFAULT_CUE_X = 248;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const RACK_APEX_X = 922;
const RACK_APEX_Y = TABLE_HEIGHT / 2;
const RACK_ROW_STEP_X = BALL_DIAMETER * 0.866;
const RACK_SPACING = BALL_DIAMETER * 1.0;
const POCKETS = [
  { id: 1, x: 54, y: 42 },
  { id: 2, x: TABLE_WIDTH / 2, y: 28 },
  { id: 3, x: TABLE_WIDTH - 54, y: 42 },
  { id: 4, x: 54, y: TABLE_HEIGHT - 42 },
  { id: 5, x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - 28 },
  { id: 6, x: TABLE_WIDTH - 54, y: TABLE_HEIGHT - 42 },
] as const;
const OPENING_RACK = [
  [1],
  [9, 2],
  [10, 8, 11],
  [3, 14, 7, 12],
  [15, 6, 13, 4, 5],
] as const;

const POCKET_ANIM_DURATION = 480;
const POWER_MIN = 0.0009;
const POWER_RETURN_MS = 180;
const POWER_CURVE_EXPONENT = 5.1;
const AIM_SYNC_INTERVAL_MS = 22;
const PLACE_SYNC_INTERVAL_MS = 16;
const REMOTE_AIM_STALE_MS = 12000;
const REALTIME_VISUAL_SNAP_DISTANCE = 54;
const REALTIME_SNAPSHOT_QUEUE_LIMIT = 32;
const REALTIME_RENDER_DELAY_MS = 52;
const REALTIME_MAX_EXTRAPOLATION_MS = 28;
const PENDING_SHOT_VISUAL_MAX_MS = 1150;
const PENDING_SHOT_POST_IMPACT_HOLD_MS = 240;
const SNAPSHOT_RENDER_DEBUG_ENABLED = false;
const SNAPSHOT_RENDER_DEBUG_LOG_EVERY_MS = 450;
const POCKET_CAPTURE_DISTANCE = BALL_RADIUS * 1.6;
const RAIL_TRAVEL_DURATION_MS = 1100;
const RAIL_SETTLE_DELAY_MS = 110;
const CUE_RETURN_HOLD_MS = 420;
const BALL_SPRITE_SIZE = 72;
const BALL_SPRITE_PHASE_BUCKETS = 96;

function logRenderSnapshotDebug(payload: Record<string, unknown>) {
  if (!SNAPSHOT_RENDER_DEBUG_ENABLED) return;
  console.log('[sinuca-snapshot-render]', JSON.stringify(payload));
}

type ShotInput = {
  angle: number;
  power: number;
  cueX?: number | null;
  cueY?: number | null;
  calledPocket?: number | null;
  spinX?: number | null;
  spinY?: number | null;
};

type Props = {
  room: RoomSnapshot;
  game: GameSnapshot;
  currentUserId: string;
  shootBusy: boolean;
  exitBusy: boolean;
  opponentAim: AimStateSnapshot | null;
  onShoot: (shot: ShotInput) => Promise<void>;
  onShotDebugEvent?: (event: ShotPipelineDebugEvent) => void;
  onAimStateChange?: (aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; seq?: number; mode: AimPointerMode }) => void;
  onExit: () => void;
  onRematchReady?: () => void;
};

type PointerMode = "idle" | "aim" | "place" | "power";
type LocalPoint = { x: number; y: number };

type AimPreview = {
  endX: number;
  endY: number;
  hitBall: GameBallSnapshot | null;
  contactX: number | null;
  contactY: number | null;
  cueDeflectX: number | null;
  cueDeflectY: number | null;
  targetGuideX: number | null;
  targetGuideY: number | null;
  targetGuideScale: number;
  hitFullness: number;
  cueTangentX: number | null;
  cueTangentY: number | null;
};

type PocketAnimation = {
  ball: GameBallSnapshot;
  pocketX: number;
  pocketY: number;
  startedAt: number;
};

type RailBallAnimation = {
  id: string;
  number: number;
  lane: number;
  slot: number;
  state: "travel" | "settled";
};

type BallSpinState = {
  phase: number;
  axis: number;
  lastX: number;
  lastY: number;
  lastSeenAt: number;
  phaseVelocity: number;
  visualSpeed: number;
  labelDepth: number;
};

type SnapshotVelocity = { x: number; y: number; pocketed: boolean };
type RealtimeSnapshotEntry = {
  balls: GameBallSnapshot[];
  revision: number;
  receivedAt: number;
  serverAt: number;
  velocities: Record<string, SnapshotVelocity>;
};

function drawRoundedBandPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = clamp(radius, 0, Math.min(width, height) * 0.5);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function cleanName(name: string) {
  return (name || "jogador").replace(/^@+/, "").trim() || "jogador";
}

function playerInitials(player: RoomPlayer | null | undefined) {
  const source = cleanName(player?.displayName ?? "J");
  const pieces = source.split(/\s+/).filter(Boolean).slice(0, 2);
  return pieces.map((piece) => piece[0]?.toUpperCase() ?? "").join("") || source[0]?.toUpperCase() || "J";
}

function ballColor(number: number) {
  const map: Record<number, string> = {
    1: "#f1d54f", 2: "#3761e2", 3: "#d34b44", 4: "#7948cb",
    5: "#f09236", 6: "#2ca65a", 7: "#6c2e21", 8: "#14181f",
    9: "#f1d54f", 10: "#3761e2", 11: "#d34b44", 12: "#7948cb",
    13: "#f09236", 14: "#2ca65a", 15: "#6c2e21",
  };
  return map[number] ?? "#f6fbff";
}

function groupOfNumber(number: number): BallGroup | null {
  if (number >= 1 && number <= 7) return "solids";
  if (number >= 9 && number <= 15) return "stripes";
  return null;
}

function remainingForGroupPreview(balls: GameBallSnapshot[], group: BallGroup | null) {
  if (!group) return 7;
  return balls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === group).length;
}

function resolveTargetForPreview(
  balls: GameBallSnapshot[],
  hostGroup: BallGroup | null,
  guestGroup: BallGroup | null,
  currentUserId: string,
  hostUserId: string,
) {
  if (!hostGroup || !guestGroup) return null as BallGroup | "eight" | null;
  const myGroup = currentUserId === hostUserId ? hostGroup : guestGroup;
  return remainingForGroupPreview(balls, myGroup) === 0 ? "eight" : myGroup;
}

function isAimTargetIllegal(
  preview: AimPreview | null,
  balls: GameBallSnapshot[],
  hostGroup: BallGroup | null,
  guestGroup: BallGroup | null,
  currentUserId: string,
  hostUserId: string,
) {
  if (!preview?.hitBall) return false;
  const target = resolveTargetForPreview(balls, hostGroup, guestGroup, currentUserId, hostUserId);
  if (!target) return preview.hitBall.number === 8;
  if (target === "eight") return preview.hitBall.number !== 8;
  return groupOfNumber(preview.hitBall.number) !== target;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function lerp(from: number, to: number, t: number) {
  return from + (to - from) * t;
}

function lerpAngle(current: number, target: number, factor: number) {
  const delta = Math.atan2(Math.sin(target - current), Math.cos(target - current));
  return current + delta * factor;
}

function clearQueuedSfx(queue: { current: number[] }) {
  for (const id of queue.current) window.clearTimeout(id);
  queue.current = [];
}

function queueSfx(queue: { current: number[] }, delayMs: number, run: () => void) {
  const id = window.setTimeout(() => {
    queue.current = queue.current.filter((value) => value !== id);
    run();
  }, Math.max(0, delayMs));
  queue.current.push(id);
}

function clampCuePosition(x: number, y: number, breakOnly: boolean) {
  return {
    x: clamp(x, PLAY_MIN_X, breakOnly ? BREAK_MAX_X : PLAY_MAX_X),
    y: clamp(y, PLAY_MIN_Y, PLAY_MAX_Y),
  };
}

function shadeColor(hex: string, amount: number): string {
  const num = parseInt(hex.replace("#", ""), 16);
  const r = clamp((num >> 16) + amount, 0, 255);
  const g = clamp(((num >> 8) & 0xff) + amount, 0, 255);
  const b = clamp((num & 0xff) + amount, 0, 255);
  return `rgb(${r},${g},${b})`;
}

function frameToDisplayBalls(frameBalls: GameShotFrameBall[], previousBalls: GameBallSnapshot[]) {
  const map = new Map(frameBalls.map((ball) => [ball.id, ball]));
  return previousBalls.map((ball) => {
    const next = map.get(ball.id);
    return next ? { ...ball, x: next.x, y: next.y, pocketed: next.pocketed } : ball;
  });
}

function pocketedNumbersForGroup(balls: GameBallSnapshot[], group: BallGroup | null) {
  if (!group) return [] as number[];
  return balls
    .filter((ball) => ball.pocketed && groupOfNumber(ball.number) === group)
    .map((ball) => ball.number)
    .sort((a, b) => a - b);
}

function buildSettledRailBalls(balls: GameBallSnapshot[]) {
  return balls
    .filter((ball) => ball.pocketed && ball.number > 0)
    .sort((a, b) => a.number - b.number)
    .map((ball, index) => ({
      id: ball.id,
      number: ball.number,
      lane: index % 3,
      slot: index,
      state: "settled" as const,
    }));
}

function interpolateFrameBalls(
  baseBalls: GameBallSnapshot[],
  fromFrame: GameShotFrameBall[],
  toFrame: GameShotFrameBall[],
  t: number,
) {
  const fromMap = new Map(fromFrame.map((ball) => [ball.id, ball]));
  const toMap = new Map(toFrame.map((ball) => [ball.id, ball]));
  return baseBalls.map((ball) => {
    const from = fromMap.get(ball.id) ?? toMap.get(ball.id);
    const to = toMap.get(ball.id) ?? fromMap.get(ball.id);
    if (!from && !to) return ball;
    const startX = from?.x ?? to?.x ?? ball.x;
    const startY = from?.y ?? to?.y ?? ball.y;
    const endX = to?.x ?? from?.x ?? ball.x;
    const endY = to?.y ?? from?.y ?? ball.y;
    return {
      ...ball,
      x: lerp(startX, endX, t),
      y: lerp(startY, endY, t),
      pocketed: (t < 0.5 ? from?.pocketed : to?.pocketed) ?? to?.pocketed ?? from?.pocketed ?? ball.pocketed,
    };
  });
}

function buildSnapshotVelocities(
  previousBalls: GameBallSnapshot[],
  nextBalls: GameBallSnapshot[],
  deltaMs: number,
): Record<string, SnapshotVelocity> {
  const safeDelta = Math.max(1, deltaMs);
  const previousMap = new Map(previousBalls.map((ball) => [ball.id, ball]));
  const velocities: Record<string, SnapshotVelocity> = {};
  for (const ball of nextBalls) {
    const prev = previousMap.get(ball.id);
    if (!prev || ball.pocketed || prev.pocketed) {
      velocities[ball.id] = { x: 0, y: 0, pocketed: ball.pocketed };
      continue;
    }
    velocities[ball.id] = {
      x: (ball.x - prev.x) / safeDelta,
      y: (ball.y - prev.y) / safeDelta,
      pocketed: ball.pocketed,
    };
  }
  return velocities;
}

function extrapolateSnapshotBalls(
  baseBalls: GameBallSnapshot[],
  velocities: Record<string, SnapshotVelocity>,
  deltaMs: number,
) {
  const stepMs = clamp(deltaMs, 0, REALTIME_MAX_EXTRAPOLATION_MS);
  if (stepMs <= 0) return baseBalls.map((ball) => ({ ...ball }));
  return baseBalls.map((ball) => {
    const velocity = velocities[ball.id];
    if (!velocity || ball.pocketed || velocity.pocketed) return { ...ball };
    // Don't extrapolate nearly-stopped balls — prevents oscillation at sim end
    const speed = Math.hypot(velocity.x, velocity.y);
    if (speed < 0.0005) return { ...ball };
    return {
      ...ball,
      x: ball.x + velocity.x * stepMs,
      y: ball.y + velocity.y * stepMs,
    };
  });
}

function interpolateSnapshotBalls(
  fromBalls: GameBallSnapshot[],
  targetBalls: GameBallSnapshot[],
  alpha: number,
) {
  if (!fromBalls.length) return targetBalls.map((ball) => ({ ...ball }));
  const fromMap = new Map(fromBalls.map((ball) => [ball.id, ball]));
  return targetBalls.map((ball) => {
    const prev = fromMap.get(ball.id);
    if (!prev) return { ...ball };
    if (prev.pocketed !== ball.pocketed) return { ...ball };

    const dx = ball.x - prev.x;
    const dy = ball.y - prev.y;
    const dist = Math.hypot(dx, dy);
    const isCueBall = ball.number === 0;
    const softSnapDistance = isCueBall ? REALTIME_VISUAL_SNAP_DISTANCE * 3.8 : REALTIME_VISUAL_SNAP_DISTANCE * 2.1;
    const hardSnapDistance = isCueBall ? softSnapDistance * 2.6 : softSnapDistance * 2.45;

    if (dist >= hardSnapDistance) return { ...ball };
    if (dist <= (isCueBall ? 0.42 : 0.32)) return { ...ball };

    const responseFloor = isCueBall ? 0.24 : 0.2;
    const responseCeil = isCueBall ? 0.88 : 0.82;
    const distanceBoost = clamp(dist / (isCueBall ? 18 : 14), 0, 1) * (isCueBall ? 0.34 : 0.3);
    const appliedAlpha = clamp(Math.max(alpha, responseFloor) + distanceBoost, responseFloor, responseCeil);
    const nextX = lerp(prev.x, ball.x, appliedAlpha);
    const nextY = lerp(prev.y, ball.y, appliedAlpha);
    const residual = dist * (1 - appliedAlpha);
    if (residual <= (isCueBall ? 0.4 : 0.28)) {
      return { ...ball };
    }
    return {
      ...ball,
      x: nextX,
      y: nextY,
      pocketed: ball.pocketed,
    };
  });
}

function interpolateSnapshotEntries(
  fromSnapshot: RealtimeSnapshotEntry,
  toSnapshot: RealtimeSnapshotEntry,
  t: number,
) {
  const eased = clamp(t, 0, 1);
  const t2 = eased * eased;
  const t3 = t2 * eased;
  const h00 = 2 * t3 - 3 * t2 + 1;
  const h10 = t3 - 2 * t2 + eased;
  const h01 = -2 * t3 + 3 * t2;
  const h11 = t3 - t2;
  const spanMs = Math.max(1, toSnapshot.serverAt - fromSnapshot.serverAt);
  const fromMap = new Map(fromSnapshot.balls.map((ball) => [ball.id, ball]));

  return toSnapshot.balls.map((ball) => {
    const start = fromMap.get(ball.id);
    if (!start) return { ...ball };
    const linearX = lerp(start.x, ball.x, eased);
    const linearY = lerp(start.y, ball.y, eased);
    const fromVelocity = fromSnapshot.velocities[ball.id] ?? { x: (ball.x - start.x) / spanMs, y: (ball.y - start.y) / spanMs, pocketed: ball.pocketed };
    const toVelocity = toSnapshot.velocities[ball.id] ?? fromVelocity;
    const tangent0X = fromVelocity.x * spanMs;
    const tangent0Y = fromVelocity.y * spanMs;
    const tangent1X = toVelocity.x * spanMs;
    const tangent1Y = toVelocity.y * spanMs;
    let x = h00 * start.x + h10 * tangent0X + h01 * ball.x + h11 * tangent1X;
    let y = h00 * start.y + h10 * tangent0Y + h01 * ball.y + h11 * tangent1Y;
    const guardX = Math.abs(ball.x - start.x) * 0.45 + 10;
    const guardY = Math.abs(ball.y - start.y) * 0.45 + 10;
    const minX = Math.min(start.x, ball.x) - guardX;
    const maxX = Math.max(start.x, ball.x) + guardX;
    const minY = Math.min(start.y, ball.y) - guardY;
    const maxY = Math.max(start.y, ball.y) + guardY;
    if (!Number.isFinite(x) || x < minX || x > maxX) x = linearX;
    if (!Number.isFinite(y) || y < minY || y > maxY) y = linearY;
    return {
      ...ball,
      x,
      y,
      pocketed: (eased < 0.5 ? start.pocketed : ball.pocketed),
    };
  });
}

function computePendingShotProgress(pending: {
  startedAt: number;
  angle: number;
  cueX: number;
  cueY: number;
  travelLimit: number;
  estimatedSpeedPxPerMs: number;
  impactAtMs: number | null;
}, now: number) {
  const elapsed = now - pending.startedAt;
  const heldElapsed = Math.min(elapsed, PENDING_SHOT_VISUAL_MAX_MS);
  const travelMs = Math.max(18, pending.impactAtMs ?? Math.max(32, pending.travelLimit / Math.max(0.85, pending.estimatedSpeedPxPerMs)));
  const moveProgress = clamp(heldElapsed / travelMs, 0, 1);
  const easedMove = 1 - Math.pow(1 - moveProgress, 2.4);
  const travelDistance = pending.travelLimit * easedMove;
  const expectedCueX = pending.cueX + Math.cos(pending.angle) * travelDistance;
  const expectedCueY = pending.cueY + Math.sin(pending.angle) * travelDistance;
  return {
    elapsed,
    heldElapsed,
    travelMs,
    moveProgress,
    expectedCueX,
    expectedCueY,
  };
}

function ballsNearlyMatchFrame(displayBalls: GameBallSnapshot[], frameBalls: GameShotFrameBall[], tolerance = 0.18) {
  const frameMap = new Map(frameBalls.map((ball) => [ball.id, ball]));
  for (const ball of displayBalls) {
    const frameBall = frameMap.get(ball.id);
    if (!frameBall) continue;
    if (ball.pocketed !== frameBall.pocketed) return false;
    if (Math.abs(ball.x - frameBall.x) > tolerance || Math.abs(ball.y - frameBall.y) > tolerance) return false;
  }
  return true;
}

function ballsNearlyMatchSnapshot(left: GameBallSnapshot[], right: GameBallSnapshot[], tolerance = 0.18) {
  const rightMap = new Map(right.map((ball) => [ball.id, ball]));
  for (const ball of left) {
    const other = rightMap.get(ball.id);
    if (!other) continue;
    if (ball.pocketed !== other.pocketed) return false;
    if (Math.abs(ball.x - other.x) > tolerance || Math.abs(ball.y - other.y) > tolerance) return false;
  }
  return true;
}

type RealtimeSoundCooldownState = {
  roomId: string | null;
  lastBallAt: number;
  lastCushionAt: number;
  lastPocketAt: number;
  movingIds: Set<string>;
  wallIds: Set<string>;
};

function emitRealtimeImpactSounds(
  previousBalls: GameBallSnapshot[],
  nextBalls: GameBallSnapshot[],
  now: number,
  cooldown: RealtimeSoundCooldownState,
) {
  // Only emit pocket sounds in realtime path. Ball-hit and cushion sounds
  // are unreliable here because snapshot timing is irregular — they're handled
  // properly in the playback path instead.
  const previousById = new Map(previousBalls.map((ball) => [ball.id, ball]));
  for (const ball of nextBalls) {
    const previous = previousById.get(ball.id);
    if (!previous) continue;
    if (ball.pocketed && !previous.pocketed && now - cooldown.lastPocketAt > 90) {
      SFX.pocket();
      cooldown.lastPocketAt = now;
      break;
    }
  }
}

function framesNearlyMatch(a: GameShotFrame, b: GameShotFrame, tolerance = 0.12) {
  const bMap = new Map(b.balls.map((ball) => [ball.id, ball]));
  for (const ball of a.balls) {
    const other = bMap.get(ball.id);
    if (!other) continue;
    if (ball.pocketed !== other.pocketed) return false;
    if (Math.abs(ball.x - other.x) > tolerance || Math.abs(ball.y - other.y) > tolerance) return false;
  }
  return true;
}

function trimPlaybackFrames(frames: GameShotFrame[]) {
  if (frames.length <= 2) return frames;
  let keepUntil = frames.length - 1;
  const finalFrame = frames[frames.length - 1];
  while (keepUntil > 1) {
    const candidate = frames[keepUntil - 1];
    if (!framesNearlyMatch(candidate, finalFrame, 0.12)) break;
    keepUntil -= 1;
  }
  return frames.slice(0, keepUntil + 1);
}

function buildOpeningBalls(source: GameBallSnapshot[]) {
  const cueSource = source.find((ball) => ball.number === 0) ?? { id: "ball-0", number: 0, x: DEFAULT_CUE_X, y: DEFAULT_CUE_Y, pocketed: false };
  const cue = {
    ...cueSource,
    ...clampCuePosition(
      Number.isFinite(cueSource.x) ? cueSource.x : DEFAULT_CUE_X,
      Number.isFinite(cueSource.y) ? cueSource.y : DEFAULT_CUE_Y,
      true,
    ),
    pocketed: false,
  };
  const rackBalls: GameBallSnapshot[] = [];
  OPENING_RACK.forEach((rowBalls, row) => {
    const rowX = RACK_APEX_X + row * RACK_ROW_STEP_X;
    const startY = RACK_APEX_Y - ((rowBalls.length - 1) * RACK_SPACING) / 2;
    rowBalls.forEach((number, index) => {
      rackBalls.push({ id: `ball-${number}`, number, x: rowX, y: startY + index * RACK_SPACING, pocketed: false });
    });
  });
  return [cue, ...rackBalls];
}

type TouchLike = {
  identifier: number;
  clientY: number;
};

type TouchListLike = {
  readonly length: number;
  item(index: number): TouchLike | null;
  [index: number]: TouchLike;
};

function findTouchById(touches: TouchListLike, identifier: number | null): TouchLike | null {
  if (identifier === null) return touches[0] ?? null;
  for (let index = 0; index < touches.length; index += 1) {
    const touch = touches.item(index);
    if (touch && touch.identifier === identifier) return touch;
  }
  return null;
}

function pointInCircle(point: LocalPoint, circleX: number, circleY: number, radius: number) {
  return Math.hypot(point.x - circleX, point.y - circleY) <= radius;
}

function estimateCueAngle(cueBall: GameBallSnapshot, balls: GameBallSnapshot[]) {
  const candidates = balls.filter((ball) => !ball.pocketed && ball.number !== 0);
  if (!candidates.length) return 0;
  let best = candidates[0];
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const ball of candidates) {
    const distance = Math.hypot(ball.x - cueBall.x, ball.y - cueBall.y);
    if (distance < bestDistance) {
      best = ball;
      bestDistance = distance;
    }
  }
  return Math.atan2(best.y - cueBall.y, best.x - cueBall.x);
}

function updateBallSpinCache(cache: Map<string, BallSpinState>, balls: GameBallSnapshot[], now: number) {
  const seen = new Set<string>();
  for (const ball of balls) {
    if (ball.pocketed) continue;
    seen.add(ball.id);
    const current = cache.get(ball.id);
    if (!current) {
      cache.set(ball.id, {
        phase: 0,
        axis: 0,
        lastX: ball.x,
        lastY: ball.y,
        lastSeenAt: now,
        phaseVelocity: 0,
        visualSpeed: 0,
        labelDepth: 1,
      });
      continue;
    }

    const elapsedMs = Math.max(1, now - current.lastSeenAt);
    const elapsedFrames = Math.max(1, elapsedMs / (1000 / 60));
    const dx = ball.x - current.lastX;
    const dy = ball.y - current.lastY;
    const rawDistance = Math.hypot(dx, dy);

    // Teleport detection: large jumps from state sync — don't spike spin
    const TELEPORT_THRESHOLD = 18.0 * elapsedFrames;
    if (rawDistance > TELEPORT_THRESHOLD) {
      current.lastX = ball.x;
      current.lastY = ball.y;
      current.lastSeenAt = now;
      current.phaseVelocity *= 0.6;
      current.visualSpeed *= 0.5;
      continue;
    }

    const distance = rawDistance;
    // Higher threshold: float noise from interpolation shouldn't trigger spin
    const moving = distance > 0.08;

    // The stripe/number motion is animated along the local Y axis in drawStripeBand()/drawBallLabel().
    // To make that apparent roll follow the actual table translation, the rendered spin axis must be
    // perpendicular to the travel direction, not aligned with it.
    if (moving) {
      const movementAngle = Math.atan2(dy, dx);
      const targetAxis = movementAngle - Math.PI * 0.5;
      // Snap fast at high speed so stripe follows direction changes immediately
      const axisBlend = distance > 1.2 ? 0.98 : distance > 0.45 ? 0.88 : 0.62;
      current.axis = lerpAngle(current.axis, targetAxis, axisBlend);
    }

    if (moving) {
      const projectedTravelX = Math.cos(current.axis + Math.PI * 0.5);
      const projectedTravelY = Math.sin(current.axis + Math.PI * 0.5);
      const signedDistance = dx * projectedTravelX + dy * projectedTravelY;
      const phaseDistance = Math.abs(signedDistance) > distance * 0.22 ? signedDistance : distance;
      const measuredPhaseVelocity = phaseDistance / (BALL_VISUAL_RADIUS * 1.04 * elapsedFrames);
      // Almost 1:1 with real movement — 1 frame delay instead of 4
      current.phaseVelocity = lerp(current.phaseVelocity, measuredPhaseVelocity, 0.92);
      current.visualSpeed = lerp(current.visualSpeed, distance / elapsedFrames, 0.9);
      current.lastX = ball.x;
      current.lastY = ball.y;
    } else {
      // Continuous decay based on speed — no binary jump
      const speedRatio = clamp(current.visualSpeed / 0.5, 0, 1);
      const decay = lerp(0.75, 0.96, speedRatio);
      current.phaseVelocity *= decay;
      current.visualSpeed *= decay;
      if (Math.abs(current.phaseVelocity) < 0.0005 && current.visualSpeed < 0.003) {
        current.phaseVelocity = 0;
        current.visualSpeed = 0;
      }
    }

    current.phase = normalizePhase(current.phase + current.phaseVelocity * elapsedFrames);
    current.labelDepth = Math.cos(current.phase);
    current.lastSeenAt = now;
  }

  for (const [id, spin] of cache) {
    if (!seen.has(id) && now - spin.lastSeenAt > 800) cache.delete(id);
  }
}

function hexToRgb(hex: string) {
  const normalized = hex.replace('#', '');
  const value = parseInt(normalized, 16);
  return {
    r: (value >> 16) & 0xff,
    g: (value >> 8) & 0xff,
    b: value & 0xff,
  };
}

// ─── Fast gradient-based stripe rendering (replaces pixel-loop) ───────────
// Much lower CPU cost, smooth animation, no frame stutter.
function drawStripeBand(
  ctx: CanvasRenderingContext2D,
  r: number,
  color: string,
  phase: number,
  scale: number,
) {
  // 3D sphere projection: the stripe is an equatorial band on a sphere.
  // As the sphere rotates (phase), the band wraps around:
  // - phase=0: band centered, full width (facing viewer)
  // - phase=±π/2: band at edge, very thin (wrapping around side)
  // - phase=±π: band on back, hidden

  const cosP = Math.cos(phase);
  const sinP = Math.sin(phase);

  // Band center position (shifts along rotation axis in projected space)
  const bandCenterY = sinP * r * 0.52;

  // Apparent band half-height: max when facing (cosP=±1), min when wrapping (cosP≈0)
  const facingWidth = r * 0.52;
  const apparentHalfH = Math.abs(cosP) * facingWidth;
  if (apparentHalfH < r * 0.03) return; // Band is on the back, skip drawing

  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, r - 0.3 * scale, 0, Math.PI * 2);
  ctx.clip();

  // Draw band with curved edges to simulate wrapping around sphere surface
  const topY = bandCenterY - apparentHalfH;
  const botY = bandCenterY + apparentHalfH;
  // Edges curve inward more when band is near the sphere edge
  const edgeCurve = (1 - Math.abs(cosP)) * r * 0.28 + Math.abs(cosP) * r * 0.08;

  ctx.beginPath();
  ctx.moveTo(-r, topY);
  ctx.quadraticCurveTo(0, topY - edgeCurve, r, topY);
  ctx.lineTo(r, botY);
  ctx.quadraticCurveTo(0, botY + edgeCurve, -r, botY);
  ctx.closePath();

  // Gradient: brighter at center of sphere, darker at edges (curvature)
  const g = ctx.createRadialGradient(0, bandCenterY, 0, 0, bandCenterY, r * 1.1);
  g.addColorStop(0, shadeColor(color, 22));
  g.addColorStop(0.5, color);
  g.addColorStop(0.85, shadeColor(color, -30));
  g.addColorStop(1, shadeColor(color, -50));
  ctx.fillStyle = g;
  ctx.fill();

  // Soft edge fade overlay
  const edgeFade = ctx.createLinearGradient(0, topY - edgeCurve, 0, botY + edgeCurve);
  edgeFade.addColorStop(0, "rgba(255,255,255,0.12)");
  edgeFade.addColorStop(0.15, "rgba(255,255,255,0)");
  edgeFade.addColorStop(0.85, "rgba(255,255,255,0)");
  edgeFade.addColorStop(1, "rgba(0,0,0,0.08)");
  ctx.fillStyle = edgeFade;
  ctx.fill();

  ctx.restore();
}

function drawBallLabel(
  ctx: CanvasRenderingContext2D,
  ball: GameBallSnapshot,
  r: number,
  phase: number,
  scale: number,
  color: string,
  isStripe: boolean,
) {
  const labelDepth = Math.cos(phase);
  if (labelDepth < -0.14) return;
  const labelY = -Math.sin(phase) * r * 0.74;
  const diskScaleY = 0.64 + Math.max(0, labelDepth) * 0.34;
  const diskAlpha = clamp(0.22 + (labelDepth + 0.14) * 0.86, 0.22, 1);
  const diskR = r * (isStripe ? 0.4 : 0.43);
  const diskGrad = ctx.createRadialGradient(0, labelY - diskR * 0.2, 0, 0, labelY, diskR);
  diskGrad.addColorStop(0, "#ffffff");
  diskGrad.addColorStop(1, "#e7eef5");

  ctx.save();
  ctx.globalAlpha = diskAlpha;
  ctx.translate(0, labelY);
  ctx.scale(1, diskScaleY);
  ctx.beginPath();
  ctx.arc(0, 0, diskR, 0, Math.PI * 2);
  ctx.fillStyle = diskGrad;
  ctx.fill();
  ctx.restore();

  const fontSize = clamp(Math.round((ball.number >= 10 ? 7 : 8.5) * scale), 5, 18);
  ctx.save();
  ctx.globalAlpha = clamp(diskAlpha + 0.06, 0.28, 1);
  ctx.translate(0, labelY);
  ctx.scale(1, diskScaleY);
  ctx.font = `700 ${fontSize}px Inter, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = ball.number === 8 ? "#0a0c12" : "#1a1e2a";
  ctx.fillText(String(ball.number), 0, 0.5 * scale);
  ctx.restore();
}



const BALL_SPRITE_CACHE = new Map<string, HTMLCanvasElement>();

function normalizePhase(phase: number) {
  const full = Math.PI * 2;
  let normalized = phase % full;
  if (normalized < 0) normalized += full;
  return normalized;
}

function getBallPhaseBucket(number: number, phase: number) {
  if (number === 0) return 0;
  const normalized = normalizePhase(phase);
  return Math.round((normalized / (Math.PI * 2)) * (BALL_SPRITE_PHASE_BUCKETS - 1));
}

function drawBallFaceSprite(
  ctx: CanvasRenderingContext2D,
  number: number,
  r: number,
  phase: number,
) {
  const color = ballColor(number);
  const isStripe = number >= 9;
  const ball = { id: `sprite-${number}`, number, x: 0, y: 0, pocketed: false } satisfies GameBallSnapshot;

  const baseGrad = ctx.createRadialGradient(-r * 0.3, -r * 0.3, r * 0.05, 0, 0, r * 1.3);
  if (number === 0) {
    baseGrad.addColorStop(0, "#ffffff");
    baseGrad.addColorStop(0.45, "#e8f0f8");
    baseGrad.addColorStop(1, "#b0c5da");
  } else if (number === 8) {
    baseGrad.addColorStop(0, "#4a5260");
    baseGrad.addColorStop(0.3, "#1e2330");
    baseGrad.addColorStop(1, "#050709");
  } else if (isStripe) {
    baseGrad.addColorStop(0, "#ffffff");
    baseGrad.addColorStop(0.65, "#f4f7fb");
    baseGrad.addColorStop(1, "#ccd6e2");
  } else {
    baseGrad.addColorStop(0, "#fff8d8");
    baseGrad.addColorStop(0.18, color);
    baseGrad.addColorStop(1, shadeColor(color, -55));
  }

  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = baseGrad;
  ctx.fill();

  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, r - 0.45, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(0,0,0,0.18)";
  ctx.lineWidth = 0.9;
  ctx.stroke();
  ctx.restore();

  if (number > 0) {
    if (isStripe) drawStripeBand(ctx, r, color, phase, 1);
    drawBallLabel(ctx, ball, r, phase, 1, color, isStripe);
  }

  const shadowGrad = ctx.createRadialGradient(r * 0.14, r * 0.28, r * 0.1, 0, r * 0.26, r * 1.04);
  shadowGrad.addColorStop(0, "rgba(10, 12, 18, 0)");
  shadowGrad.addColorStop(1, "rgba(10, 12, 18, 0.16)");
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = shadowGrad;
  ctx.fill();

  const specGrad = ctx.createRadialGradient(-r * 0.34, -r * 0.38, 0, -r * 0.28, -r * 0.3, r * 0.55);
  specGrad.addColorStop(0, "rgba(255, 255, 255, 0.72)");
  specGrad.addColorStop(0.35, "rgba(255, 255, 255, 0.22)");
  specGrad.addColorStop(1, "rgba(255, 255, 255, 0)");
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = specGrad;
  ctx.fill();
}

function getBallSprite(number: number, phase: number) {
  if (typeof document === "undefined") return null;
  const bucket = getBallPhaseBucket(number, phase);
  const key = `${number}:${bucket}`;
  const cached = BALL_SPRITE_CACHE.get(key);
  if (cached) return cached;

  const sprite = document.createElement("canvas");
  sprite.width = BALL_SPRITE_SIZE;
  sprite.height = BALL_SPRITE_SIZE;
  const ctx = sprite.getContext("2d");
  if (!ctx) return null;

  const r = BALL_SPRITE_SIZE / 2 - 2;
  const normalizedPhase = number === 0
    ? 0
    : (bucket / Math.max(1, BALL_SPRITE_PHASE_BUCKETS - 1)) * Math.PI * 2;
  ctx.translate(BALL_SPRITE_SIZE / 2, BALL_SPRITE_SIZE / 2);
  drawBallFaceSprite(ctx, number, r, normalizedPhase);
  BALL_SPRITE_CACHE.set(key, sprite);
  return sprite;
}

// ─── Aim preview computation ───────────────────────────────────────────────

function findFirstBoundaryHit(cueBall: GameBallSnapshot, angle: number) {
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  const limits: number[] = [];
  if (dx > 0.0001) limits.push((PLAY_MAX_X - cueBall.x) / dx);
  if (dx < -0.0001) limits.push((PLAY_MIN_X - cueBall.x) / dx);
  if (dy > 0.0001) limits.push((PLAY_MAX_Y - cueBall.y) / dy);
  if (dy < -0.0001) limits.push((PLAY_MIN_Y - cueBall.y) / dy);
  const distance = Math.min(...limits.filter((value) => Number.isFinite(value) && value > 0));
  return { x: cueBall.x + dx * distance, y: cueBall.y + dy * distance, distance };
}

function computeAimPreview(cueBall: GameBallSnapshot, balls: GameBallSnapshot[], angle: number): AimPreview {
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  const boundary = findFirstBoundaryHit(cueBall, angle);
  let hitBall: GameBallSnapshot | null = null;
  let hitDistance = boundary.distance;
  let contactX: number | null = null;
  let contactY: number | null = null;
  let cueDeflectX: number | null = null;
  let cueDeflectY: number | null = null;
  let targetGuideX: number | null = null;
  let targetGuideY: number | null = null;
  let targetGuideScale = 1;
  let hitFullness = 1;
  let cueTangentX: number | null = null;
  let cueTangentY: number | null = null;

  for (const ball of balls) {
    if (ball.pocketed || ball.number === 0) continue;
    const relX = ball.x - cueBall.x;
    const relY = ball.y - cueBall.y;
    const projection = relX * dx + relY * dy;
    if (projection <= 1) continue;
    const perpendicularSq = relX * relX + relY * relY - projection * projection;
    const limit = BALL_DIAMETER * BALL_DIAMETER;
    if (perpendicularSq < 0 || perpendicularSq > limit) continue;
    const approach = projection - Math.sqrt(Math.max(0, limit - perpendicularSq));
    if (approach < hitDistance) {
      hitDistance = approach;
      hitBall = ball;
      contactX = cueBall.x + dx * approach;
      contactY = cueBall.y + dy * approach;
    }
  }

  if (hitBall && contactX !== null && contactY !== null) {
    const nx = hitBall.x - contactX;
    const ny = hitBall.y - contactY;
    const nlen = Math.hypot(nx, ny) || 1;
    const nnx = nx / nlen;
    const nny = ny / nlen;
    const dotN = clamp(dx * nnx + dy * nny, 0, 1);
    hitFullness = clamp(dotN, 0.16, 1);
    targetGuideScale = clamp(Math.pow(hitFullness, 0.92), 0.14, 1);
    const targetGuideLength = lerp(16, 72, targetGuideScale);
    targetGuideX = hitBall.x + nnx * targetGuideLength;
    targetGuideY = hitBall.y + nny * targetGuideLength;
    const remVx = dx - dotN * nnx;
    const remVy = dy - dotN * nny;
    const remLen = Math.hypot(remVx, remVy);
    const DEFLECT_DIST = 100;
    if (remLen > 0.05) {
      cueTangentX = remVx / remLen;
      cueTangentY = remVy / remLen;
      cueDeflectX = contactX + cueTangentX * DEFLECT_DIST;
      cueDeflectY = contactY + cueTangentY * DEFLECT_DIST;
    } else {
      cueTangentX = 0;
      cueTangentY = 0;
      cueDeflectX = contactX;
      cueDeflectY = contactY;
    }
  }

  return {
    endX: cueBall.x + dx * hitDistance,
    endY: cueBall.y + dy * hitDistance,
    hitBall,
    contactX,
    contactY,
    cueDeflectX,
    cueDeflectY,
    targetGuideX,
    targetGuideY,
    targetGuideScale,
    hitFullness,
    cueTangentX,
    cueTangentY,
  };
}

// ─── Canvas ball rendering (high quality 3D with numbers/stripes) ──────────

function drawBall(
  ctx: CanvasRenderingContext2D,
  ball: GameBallSnapshot,
  scale = 1,
  spin: BallSpinState | undefined = undefined,
) {
  const r = BALL_VISUAL_RADIUS * scale;
  const color = ballColor(ball.number);
  const spinPhase = spin?.phase ?? 0;
  const spinAxis = spin?.axis ?? 0;

  ctx.save();
  ctx.translate(ball.x, ball.y);

  ctx.shadowColor = "rgba(0, 0, 0, 0.48)";
  ctx.shadowBlur = 9 * scale;
  ctx.shadowOffsetX = 0.9 * scale;
  ctx.shadowOffsetY = 4.5 * scale;

  const baseGrad = ctx.createRadialGradient(-r * 0.3, -r * 0.3, r * 0.05, 0, 0, r * 1.3);
  if (ball.number === 0) {
    baseGrad.addColorStop(0, "#ffffff");
    baseGrad.addColorStop(0.45, "#e8f0f8");
    baseGrad.addColorStop(1, "#b0c5da");
  } else if (ball.number === 8) {
    baseGrad.addColorStop(0, "#4a5260");
    baseGrad.addColorStop(0.3, "#1e2330");
    baseGrad.addColorStop(1, "#050709");
  } else if (ball.number >= 9) {
    baseGrad.addColorStop(0, "#ffffff");
    baseGrad.addColorStop(0.65, "#f4f7fb");
    baseGrad.addColorStop(1, "#ccd6e2");
  } else {
    baseGrad.addColorStop(0, "#fff8d8");
    baseGrad.addColorStop(0.18, color);
    baseGrad.addColorStop(1, shadeColor(color, -55));
  }

  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = baseGrad;
  ctx.fill();
  ctx.shadowColor = "transparent";

  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, r - 0.45 * scale, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(0,0,0,0.18)";
  ctx.lineWidth = 0.9 * scale;
  ctx.stroke();
  ctx.restore();

  if (ball.number > 0) {
    const isStripe = ball.number >= 9;
    ctx.save();
    ctx.rotate(spinAxis);
    if (isStripe) drawStripeBand(ctx, r, color, spinPhase, scale);
    drawBallLabel(ctx, ball, r, spinPhase, scale, color, isStripe);
    ctx.restore();
  }

  const shadowGrad = ctx.createRadialGradient(r * 0.14, r * 0.28, r * 0.1, 0, r * 0.26, r * 1.04);
  shadowGrad.addColorStop(0, "rgba(10, 12, 18, 0)");
  shadowGrad.addColorStop(1, "rgba(10, 12, 18, 0.16)");
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = shadowGrad;
  ctx.fill();

  const specGrad = ctx.createRadialGradient(-r * 0.34, -r * 0.38, 0, -r * 0.28, -r * 0.3, r * 0.55);
  specGrad.addColorStop(0, "rgba(255, 255, 255, 0.72)");
  specGrad.addColorStop(0.35, "rgba(255, 255, 255, 0.22)");
  specGrad.addColorStop(1, "rgba(255, 255, 255, 0)");
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = specGrad;
  ctx.fill();

  ctx.restore();
}

// ─── Pocket animation ─────────────────────────────────────────────────────

function drawPocketAnimation(
  ctx: CanvasRenderingContext2D,
  anim: PocketAnimation,
  now: number,
  spin: BallSpinState | undefined = undefined,
) {
  const elapsed = now - anim.startedAt;
  const t = clamp(elapsed / POCKET_ANIM_DURATION, 0, 1);
  // Gravity-like easing: slow start, fast pull into pocket
  const gravityT = t < 0.3 ? t * t * (1 / 0.09) * 0.09 : 0.09 + (1 - 0.09) * Math.pow((t - 0.3) / 0.7, 1.6);
  const eased = clamp(gravityT, 0, 1);
  // Scale shrinks from 1 → 0, accelerating at the end
  const scale = lerp(1, 0.02, Math.pow(eased, 0.8));
  // Alpha fades out in the last 40%
  const alpha = t < 0.6 ? 1 : lerp(1, 0, (t - 0.6) / 0.4);
  if (alpha <= 0.01 || scale <= 0.02) return;
  // Spiral offset: ball spirals slightly as it falls in
  const spiralAngle = eased * Math.PI * 2.5;
  const spiralRadius = (1 - eased) * BALL_VISUAL_RADIUS * 0.6;
  const spiralX = Math.cos(spiralAngle) * spiralRadius;
  const spiralY = Math.sin(spiralAngle) * spiralRadius;
  ctx.save();
  ctx.globalAlpha = alpha;
  const ball = {
    ...anim.ball,
    x: lerp(anim.ball.x, anim.pocketX, eased) + spiralX * (1 - eased),
    y: lerp(anim.ball.y, anim.pocketY, eased) + spiralY * (1 - eased),
  };
  drawBall(ctx, ball, scale, spin);
  ctx.restore();
}

// ─── Aim guide (reference-style: clean line + ghost ball) ─────────────────

// Aim line only — drawn BEFORE balls
function drawAimLine(ctx: CanvasRenderingContext2D, cueBall: GameBallSnapshot, preview: AimPreview, illegalTarget: boolean) {
  const hasHit = preview.contactX !== null && preview.contactY !== null && preview.hitBall;
  const lineEndX = hasHit ? preview.contactX! : preview.endX;
  const lineEndY = hasHit ? preview.contactY! : preview.endY;

  // Soft glow
  ctx.save();
  ctx.strokeStyle = "rgba(200, 230, 255, 0.10)";
  ctx.lineWidth = 6;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  // Main aim line — SOLID, bright
  ctx.save();
  ctx.strokeStyle = "rgba(245, 250, 255, 0.88)";
  ctx.lineWidth = 2.15;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  if (!illegalTarget && hasHit && preview.hitBall && preview.targetGuideX !== null && preview.targetGuideY !== null) {
    const guideScale = clamp(preview.targetGuideScale, 0.14, 1);
    ctx.save();
    ctx.strokeStyle = "rgba(245, 250, 255, 0.88)";
    ctx.lineWidth = lerp(1.05, 2.15, guideScale);
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(preview.hitBall.x, preview.hitBall.y);
    ctx.lineTo(preview.targetGuideX, preview.targetGuideY);
    ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.72)";
    ctx.lineWidth = lerp(1.0, 2.15, guideScale);
    ctx.beginPath();
    ctx.arc(preview.hitBall.x, preview.hitBall.y, lerp(BALL_VISUAL_RADIUS * 0.42, BALL_VISUAL_RADIUS + 1.5, guideScale), 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

// Ghost ball circle — drawn AFTER balls so it's visible on top
function drawIllegalAimMarker(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, 8.5, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255, 88, 88, 0.98)";
  ctx.lineWidth = 2.6;
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x - 5.8, y + 5.8);
  ctx.lineTo(x + 5.8, y - 5.8);
  ctx.strokeStyle = "rgba(255, 88, 88, 0.98)";
  ctx.lineCap = "round";
  ctx.lineWidth = 2.8;
  ctx.stroke();
  ctx.restore();
}

function drawGhostBall(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  preview: AimPreview,
  powerRatio: number,
  illegalTarget: boolean,
) {
  const hasHit = preview.contactX !== null && preview.contactY !== null && preview.hitBall;
  const dx = Math.cos(Math.atan2(preview.endY - cueBall.y, preview.endX - cueBall.x));
  const dy = Math.sin(Math.atan2(preview.endY - cueBall.y, preview.endX - cueBall.x));

  if (hasHit) {
    const ghostX = preview.contactX!;
    const ghostY = preview.contactY!;

    if (illegalTarget) {
      drawIllegalAimMarker(ctx, ghostX, ghostY);
      return;
    }

    const ghostScale = clamp(0.18 + preview.hitFullness * 0.82, 0.18, 1);
    const ghostRadius = BALL_VISUAL_RADIUS * ghostScale;

    ctx.save();
    ctx.beginPath();
    ctx.arc(ghostX, ghostY, ghostRadius + 2, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(200, 240, 255, 0.25)";
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.arc(ghostX, ghostY, ghostRadius, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.92)";
    ctx.lineWidth = 3.05;
    ctx.stroke();
    ctx.restore();

    const tangentX = preview.cueTangentX ?? dx;
    const tangentY = preview.cueTangentY ?? dy;
    const cueGuideTravel = lerp(62, 176, powerRatio) * lerp(0.28, 1.0, preview.hitFullness);
    const cueGuideStartX = ghostX + tangentX * 10;
    const cueGuideStartY = ghostY + tangentY * 10;
    const cueGuideEndX = ghostX + tangentX * cueGuideTravel;
    const cueGuideEndY = ghostY + tangentY * cueGuideTravel;

    ctx.save();
    ctx.strokeStyle = "rgba(248, 252, 255, 0.82)";
    ctx.lineWidth = clamp(0.95 + preview.hitFullness * 1.25, 0.95, 2.35);
    ctx.lineCap = "round";
    ctx.setLineDash([10, 9]);
    ctx.beginPath();
    ctx.moveTo(cueGuideStartX, cueGuideStartY);
    ctx.lineTo(cueGuideEndX, cueGuideEndY);
    ctx.stroke();
    ctx.restore();
    return;
  }

  if (illegalTarget) {
    drawIllegalAimMarker(ctx, preview.endX, preview.endY);
  }
}

// ─── Cue stick rendering ──────────────────────────────────────────────────

function drawCue(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  aimAngle: number,
  pullRatio: number,
  cueSprite: HTMLImageElement,
) {
  const dirX = Math.cos(aimAngle);
  const dirY = Math.sin(aimAngle);
  const cueGap = BALL_RADIUS + 14 + pullRatio * 72;
  const cueLength = 440;
  const drawHeight = cueSprite.complete && cueSprite.naturalWidth
    ? Math.max(6, cueLength * (cueSprite.naturalHeight / cueSprite.naturalWidth) * 0.6)
    : 6;

  ctx.save();
  ctx.translate(cueBall.x - dirX * cueGap, cueBall.y - dirY * cueGap);
  ctx.rotate(aimAngle);
  ctx.shadowColor = "rgba(0, 0, 0, 0.25)";
  ctx.shadowBlur = 6;
  ctx.shadowOffsetY = 1.5;
  if (cueSprite.complete && cueSprite.naturalWidth) {
    ctx.drawImage(cueSprite, -cueLength, -drawHeight / 2, cueLength, drawHeight);
  } else {
    // Fallback cue drawing
    const grad = ctx.createLinearGradient(-cueLength, 0, 0, 0);
    grad.addColorStop(0, "#8b6914");
    grad.addColorStop(0.7, "#c9a03c");
    grad.addColorStop(0.95, "#e8d088");
    grad.addColorStop(1, "#f0f0f0");
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2.8;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-cueLength, 0);
    ctx.stroke();
  }
  ctx.restore();
}

function drawRemoteAimOverlay(
  ctx: CanvasRenderingContext2D,
  cueSprite: HTMLImageElement,
  cueBall: GameBallSnapshot,
  aimAngle: number,
  preview: AimPreview | null,
  pullRatio: number,
  mode: AimPointerMode,
) {
  const lineEndX = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactX : preview?.endX ?? (cueBall.x + Math.cos(aimAngle) * 420);
  const lineEndY = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactY : preview?.endY ?? (cueBall.y + Math.sin(aimAngle) * 420);
  const showPlacementRing = mode === "place";
  const showGuide = mode !== "place";

  if (showPlacementRing) {
    ctx.save();
    ctx.globalAlpha = 0.72;
    drawBall(ctx, cueBall, 1);
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.82;
    ctx.setLineDash([7, 5]);
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, BALL_VISUAL_RADIUS + 8, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.restore();
  }

  if (showGuide) {
    if (preview) {
      ctx.save();
      ctx.globalAlpha = mode === "power" ? 0.48 : 0.4;
      drawAimLine(ctx, cueBall, preview, false);
      ctx.restore();

      ctx.save();
      ctx.globalAlpha = mode === "power" ? 0.42 : 0.34;
      drawGhostBall(ctx, cueBall, preview, clamp(pullRatio, 0.12, 1), false);
      ctx.restore();
    } else {
      ctx.save();
      ctx.globalAlpha = mode === "power" ? 0.68 : 0.62;
      ctx.strokeStyle = "rgba(244, 248, 255, 0.96)";
      ctx.lineWidth = mode === "power" ? 2.8 : 2.35;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(cueBall.x, cueBall.y);
      ctx.lineTo(lineEndX, lineEndY);
      ctx.stroke();
      ctx.restore();
    }
  }

  ctx.save();
  ctx.globalAlpha = mode === "power" ? 0.56 : mode === "aim" ? 0.5 : 0.46;
  drawCue(ctx, cueBall, aimAngle, pullRatio, cueSprite);
  ctx.restore();
}

// ─── Table render cache ───────────────────────────────────────────────────

function makeTableCache(image: HTMLImageElement) {
  if (!image.complete || !image.naturalWidth) return null;
  const canvas = document.createElement("canvas");
  canvas.width = TABLE_WIDTH;
  canvas.height = TABLE_HEIGHT;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.clearRect(0, 0, TABLE_WIDTH, TABLE_HEIGHT);
  ctx.drawImage(image, 0, 0, TABLE_WIDTH, TABLE_HEIGHT);
  return canvas;
}

// ─── Main draw function ───────────────────────────────────────────────────

function drawPoolTable(
  ctx: CanvasRenderingContext2D,
  tableCache: HTMLCanvasElement | null,
  cueSprite: HTMLImageElement,
  renderBalls: GameBallSnapshot[],
  cueBall: GameBallSnapshot | null,
  aimAngle: number,
  showGuide: boolean,
  pullRatio: number,
  preview: AimPreview | null,
  previewPowerRatio: number,
  illegalTarget: boolean,
  needEightCall: boolean,
  selectedPocket: number | null,
  isBallInHand: boolean,
  pocketAnimations: PocketAnimation[],
  now: number,
  ballSpinCache: Map<string, BallSpinState>,
  remoteOverlay: { cueBall: GameBallSnapshot; aimAngle: number; preview: AimPreview | null; pullRatio: number; mode: AimPointerMode } | null,
) {
  ctx.clearRect(0, 0, TABLE_WIDTH, TABLE_HEIGHT);

  // Table background
  if (tableCache) {
    ctx.drawImage(tableCache, 0, 0, TABLE_WIDTH, TABLE_HEIGHT);
  }

  // Called pocket highlight
  if (needEightCall && selectedPocket !== null) {
    const selected = POCKETS.find((pocket) => pocket.id === selectedPocket) ?? null;
    if (selected) {
      ctx.beginPath();
      ctx.arc(selected.x, selected.y, 21, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255, 244, 163, 0.96)";
      ctx.lineWidth = 3.4;
      ctx.stroke();
    }
  }

  // STEP 1: Local aim line + cue BEFORE balls.
  if (cueBall && showGuide && preview) {
    drawAimLine(ctx, cueBall, preview, illegalTarget);
    drawCue(ctx, cueBall, aimAngle, pullRatio, cueSprite);
  }

  // STEP 2: All balls
  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    drawBall(ctx, ball, 1, ballSpinCache.get(ball.id));
  }

  // STEP 3: Remote overlay AFTER balls so the transparent cue/mira stay visible.
  if (remoteOverlay) {
    drawRemoteAimOverlay(ctx, cueSprite, remoteOverlay.cueBall, remoteOverlay.aimAngle, remoteOverlay.preview, remoteOverlay.pullRatio, remoteOverlay.mode);
  }

  // STEP 4: Ghost ball circle AFTER balls (so it's visible on top)
  if (cueBall && showGuide && preview) {
    drawGhostBall(ctx, cueBall, preview, previewPowerRatio, illegalTarget);
  }

  // Pocket animations
  for (const anim of pocketAnimations) {
    drawPocketAnimation(ctx, anim, now, ballSpinCache.get(anim.ball.id));
  }

  // Ball-in-hand indicator
  if (cueBall && isBallInHand) {
    ctx.save();
    ctx.setLineDash([8, 6]);
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, BALL_VISUAL_RADIUS + 9, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.42)";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.restore();
  }
}

function createImage(src: string) {
  const image = new window.Image();
  image.decoding = "async";
  image.src = src;
  return image;
}

// ─── Main component ───────────────────────────────────────────────────────

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, opponentAim, onShoot, onShotDebugEvent, onAimStateChange, onExit, onRematchReady }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [railBalls, setRailBalls] = useState<RailBallAnimation[]>(() => buildSettledRailBalls(game.balls));
  const [power, setPower] = useState(POWER_MIN);
  const [, setAimAngle] = useState(0);
  const [pointerMode, setPointerMode] = useState<PointerMode>("idle");
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState(0);
  const [selectedPocket, setSelectedPocket] = useState<number | null>(null);
  const [assetsVersion, setAssetsVersion] = useState(0);
  const [groupBanner, setGroupBanner] = useState<string | null>(null);
  const [displayedGroups, setDisplayedGroups] = useState<{ hostGroup: BallGroup | null; guestGroup: BallGroup | null }>({
    hostGroup: game.hostGroup,
    guestGroup: game.guestGroup,
  });
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const powerRailRef = useRef<HTMLDivElement | null>(null);
  const powerTrackRef = useRef<HTMLDivElement | null>(null);
  const drawLoopRef = useRef<number | null>(null);
  const tableCacheRef = useRef<HTMLCanvasElement | null>(null);
  const lastAnimatedSeqRef = useRef(0);
  const playbackRef = useRef<{
    seq: number;
    frames: { balls: GameShotFrameBall[] }[];
    startedAt: number;
    baseBalls: GameBallSnapshot[];
    finalBalls: GameBallSnapshot[];
  } | null>(null);
  const playbackSettlingRef = useRef(false);
  const pointerMovedRef = useRef(false);
  const aimDragRef = useRef<{ x: number; y: number } | null>(null);
  const aimAngleRef = useRef(0);
  const drawAimAngleRef = useRef(0);
  const powerRef = useRef(power);
  const powerReleaseGuardRef = useRef(false);
  const powerReturnAnimRef = useRef<number | null>(null);
  const powerGestureRef = useRef<{ rectTop: number; rectBottom: number; rectHeight: number } | null>(null);
  const powerPointerIdRef = useRef<number | null>(null);
  const powerTouchIdRef = useRef<number | null>(null);
  const localCuePlacementRef = useRef<{ x: number; y: number } | null>(null);
  const pointerModeRef = useRef<PointerMode>("idle");
  const pocketAnimationsRef = useRef<PocketAnimation[]>([]);
  const prevPocketedIdsRef = useRef<Set<string>>(new Set());
  const hiddenPocketIdsRef = useRef<Set<string>>(new Set(game.balls.filter((ball) => ball.pocketed).map((ball) => ball.id)));
  const railCapturedIdsRef = useRef<Set<string>>(new Set(game.balls.filter((ball) => ball.pocketed && ball.number > 0).map((ball) => ball.id)));
  const railTimersRef = useRef<number[]>([]);
  const cuePocketHoldUntilRef = useRef(0);
  const ballSpinRef = useRef<Map<string, BallSpinState>>(new Map());
  const snapAnimRef = useRef<{ startedAt: number; power: number; fired: boolean } | null>(null);
  const pendingShotVisualRef = useRef<{ startedAt: number; shotSequenceAtDispatch: number; revisionAtDispatch: number; angle: number; power: number; cueX: number; cueY: number; travelLimit: number; estimatedSpeedPxPerMs: number; impactType: "ball" | "cushion" | null; impactAtMs: number | null; firstImpactPlayed: boolean } | null>(null);
  const queuedSfxRef = useRef<number[]>([]);
  const playbackSoundStateRef = useRef<{ seq: number; frameIndex: number; lastBallAt: number; lastCushionAt: number; movingIds: Set<string>; wallIds: Set<string> }>({ seq: 0, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0, movingIds: new Set(), wallIds: new Set() });
  const realtimeSoundSnapshotRef = useRef<{ roomId: string | null; revision: number; balls: GameBallSnapshot[] }>({ roomId: game.roomId, revision: game.snapshotRevision ?? 0, balls: game.balls.map((ball) => ({ ...ball })) });
  const realtimeSoundCooldownRef = useRef<RealtimeSoundCooldownState>({ roomId: game.roomId, lastBallAt: 0, lastCushionAt: 0, lastPocketAt: 0, movingIds: new Set(), wallIds: new Set() });
  const canInteractRef = useRef(false);
  const shootBusyRef = useRef(false);
  const onShootRef = useRef(onShoot);
  const onAimStateChangeRef = useRef(onAimStateChange);
  const displayedGroupsRef = useRef(displayedGroups);
  const pendingDisplayedGroupsRef = useRef<{ hostGroup: BallGroup | null; guestGroup: BallGroup | null } | null>(null);
  const lastAimEmitAtRef = useRef(0);
  const lastAimPayloadKeyRef = useRef<string>("");
  const aimSeqRef = useRef(0);
  const realtimeVisualBallsRef = useRef<GameBallSnapshot[]>(game.balls.map((ball) => ({ ...ball })));
  const realtimeVisualLastAtRef = useRef<number>(performance.now());
  const snapshotQueueRef = useRef<RealtimeSnapshotEntry[]>([{
    balls: game.balls.map((ball) => ({ ...ball })),
    revision: game.snapshotRevision ?? 0,
    receivedAt: performance.now(),
    serverAt: game.updatedAt || Date.now(),
    velocities: {},
  }]);
  const snapshotRenderDebugRef = useRef<{ lastLoggedAt: number; lastQueueSize: number; lastMode: string }>({ lastLoggedAt: 0, lastQueueSize: 0, lastMode: 'init' });
  const remoteAimVisualRef = useRef<{ x: number; y: number; angle: number; pull: number; seq: number; initialized: boolean }>({
    x: 0,
    y: 0,
    angle: 0,
    pull: 0,
    seq: 0,
    initialized: false,
  });

  const renderStateRef = useRef({
    renderBalls: [] as GameBallSnapshot[],
    cueBall: null as GameBallSnapshot | null,
    canInteract: false,
    pointerMode: "idle" as PointerMode,
    power,
    needEightCall: false,
    selectedPocket: null as number | null,
    isBallInHand: false,
    shootBusy: false,
  });

  // Load only table and cue sprites (balls are canvas-rendered)
  const tableSprite = useMemo(() => createImage(tableAsset), []);
  const cueSprite = useMemo(() => createImage(cueAsset), []);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const isHost = currentUserId === room.hostUserId;
  const opponent = room.players.find((player) => player.userId !== currentUserId) ?? (isHost ? guest : host);
  const leftPlayer = host;
  const rightPlayer = guest;
  const leftGroup = displayedGroups.hostGroup;
  const rightGroup = displayedGroups.guestGroup;
  const myGroup = currentUserId === room.hostUserId ? displayedGroups.hostGroup : displayedGroups.guestGroup;
  const isMyTurn = game.turnUserId === currentUserId;
  const isOpenTable = !displayedGroups.hostGroup || !displayedGroups.guestGroup;

  // Asset loading
  useEffect(() => {
    const notifyLoaded = () => setAssetsVersion((current) => current + 1);
    const unregister: Array<() => void> = [];
    for (const image of [tableSprite, cueSprite]) {
      if (image.complete && image.naturalWidth) continue;
      const handle = () => notifyLoaded();
      image.addEventListener("load", handle);
      image.addEventListener("error", handle);
      unregister.push(() => { image.removeEventListener("load", handle); image.removeEventListener("error", handle); });
    }
    return () => unregister.forEach((fn) => fn());
  }, [tableSprite, cueSprite]);

  useEffect(() => { tableCacheRef.current = makeTableCache(tableSprite); }, [assetsVersion, tableSprite]);

  useEffect(() => { onAimStateChangeRef.current = onAimStateChange; }, [onAimStateChange]);
  useEffect(() => { displayedGroupsRef.current = displayedGroups; }, [displayedGroups]);

  useEffect(() => {
    const next = { hostGroup: game.hostGroup, guestGroup: game.guestGroup };
    const current = displayedGroupsRef.current;
    const changed = current.hostGroup !== next.hostGroup || current.guestGroup !== next.guestGroup;
    if (!changed) return;

    const assigningAfterPocket = current.hostGroup === null && current.guestGroup === null && next.hostGroup !== null && next.guestGroup !== null;
    if (assigningAfterPocket && animating) {
      pendingDisplayedGroupsRef.current = next;
      return;
    }

    pendingDisplayedGroupsRef.current = null;
    displayedGroupsRef.current = next;
    setDisplayedGroups(next);
  }, [animating, game.guestGroup, game.hostGroup]);

  useEffect(() => {
    if (animating || !pendingDisplayedGroupsRef.current) return;
    const next = pendingDisplayedGroupsRef.current;
    pendingDisplayedGroupsRef.current = null;
    displayedGroupsRef.current = next;
    setDisplayedGroups(next);
  }, [animating]);

  useEffect(() => {
    if (animating) return;
    if (game.status === "simulating") return;
    let nextBalls = game.balls;
    // On simulation end, prefer current visual positions to prevent jump
    if (realtimeVisualBallsRef.current.length) {
      const visualMap = new Map(realtimeVisualBallsRef.current.map(b => [b.id, b]));
      nextBalls = game.balls.map(ball => {
        const vis = visualMap.get(ball.id);
        if (!vis || ball.pocketed !== vis.pocketed) return ball;
        // Use visual position if close, server position if far (authoritative correction)
        const dist = Math.hypot(ball.x - vis.x, ball.y - vis.y);
        return dist < 8 ? { ...ball, x: vis.x, y: vis.y } : ball;
      });
    }
    if (game.ballInHandUserId === currentUserId && localCuePlacementRef.current) {
      const placed = clampCuePosition(
        localCuePlacementRef.current.x,
        localCuePlacementRef.current.y,
        game.shotSequence === 0,
      );
      nextBalls = nextBalls.map((ball) => (ball.number === 0 ? { ...ball, x: placed.x, y: placed.y, pocketed: false } : ball));
    }
    setDisplayBalls(nextBalls);
  }, [animating, currentUserId, game.ballInHandUserId, game.balls, game.shotSequence, game.status]);

  useEffect(() => {
    const now = performance.now();
    if (animating) return;
    if (game.status !== "simulating") {
      const copied = displayBalls.map((ball) => ({ ...ball }));
      realtimeVisualBallsRef.current = copied;
      realtimeVisualLastAtRef.current = now;
      snapshotQueueRef.current = [{
        balls: copied.map((ball) => ({ ...ball })),
        revision: game.snapshotRevision ?? 0,
        receivedAt: now,
        serverAt: game.updatedAt || Date.now(),
        velocities: {},
      }];
      return;
    }

    const nextRevision = game.snapshotRevision ?? 0;
    const queue = snapshotQueueRef.current;
    const incomingBalls = game.balls.map((ball) => ({ ...ball }));
    const latestEntry = queue[queue.length - 1] ?? null;
    const latestRevision = latestEntry?.revision ?? -1;

    if (!queue.length) {
      const baseBalls = (realtimeVisualBallsRef.current.length ? realtimeVisualBallsRef.current : displayBalls).map((ball) => ({ ...ball }));
      const baseServerAt = Math.max(0, (game.updatedAt || Date.now()) - (REALTIME_RENDER_DELAY_MS + 8));
      queue.push({
        balls: baseBalls,
        revision: Math.max(0, nextRevision - 1),
        receivedAt: now - (REALTIME_RENDER_DELAY_MS + 8),
        serverAt: baseServerAt,
        velocities: {},
      });
    }

    if (nextRevision < latestRevision) return;
    if (nextRevision === latestRevision) {
      if (queue.length === 1 && realtimeVisualBallsRef.current.length) {
        const liveBase = realtimeVisualBallsRef.current.map((ball) => ({ ...ball }));
        if (!ballsNearlyMatchSnapshot(queue[0].balls, liveBase, 0.12)) {
          queue[0] = {
            ...queue[0],
            balls: liveBase,
            receivedAt: now,
            serverAt: game.updatedAt || Date.now(),
          };
        }
      }
      return;
    }

    const previousEntry = queue[queue.length - 1];
    const nextServerAt = game.updatedAt || Date.now();
    const serverDeltaMs = previousEntry ? Math.max(1, nextServerAt - previousEntry.serverAt) : 0;
    const pending = pendingShotVisualRef.current;
    if (pending) {
      const incomingCueBall = incomingBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
      if (incomingCueBall) {
        const progress = computePendingShotProgress(pending, now);
        const authoritativeDistanceFromStart = Math.hypot(incomingCueBall.x - pending.cueX, incomingCueBall.y - pending.cueY);
        const authoritativeDistanceFromExpected = Math.hypot(incomingCueBall.x - progress.expectedCueX, incomingCueBall.y - progress.expectedCueY);
        const staleBootstrapSnapshot = authoritativeDistanceFromStart <= Math.max(BALL_RADIUS * 1.3, 17)
          && authoritativeDistanceFromExpected >= Math.max(9, BALL_RADIUS * 0.9)
          && progress.elapsed < PENDING_SHOT_VISUAL_MAX_MS;
        if (staleBootstrapSnapshot) {
          // Instead of rejecting the snapshot entirely, accept it but override
          // the cue ball position with the optimistic one. This way other balls
          // start moving visually during the break while the cue stays smooth.
          const optimisticCueX = progress.expectedCueX;
          const optimisticCueY = progress.expectedCueY;
          for (let i = 0; i < incomingBalls.length; i++) {
            if (incomingBalls[i].number === 0 && !incomingBalls[i].pocketed) {
              incomingBalls[i] = { ...incomingBalls[i], x: optimisticCueX, y: optimisticCueY };
            }
          }
          // Fall through to push the modified snapshot
        }
      }
    }
    const nextEntry: RealtimeSnapshotEntry = {
      balls: incomingBalls,
      revision: nextRevision,
      receivedAt: now,
      serverAt: nextServerAt,
      velocities: previousEntry
        ? buildSnapshotVelocities(previousEntry.balls, incomingBalls, serverDeltaMs)
        : {},
    };

    queue.push(nextEntry);

    const debugNow = now;
    const debugState = snapshotRenderDebugRef.current;
    if (debugNow - debugState.lastLoggedAt >= SNAPSHOT_RENDER_DEBUG_LOG_EVERY_MS || debugState.lastQueueSize !== queue.length) {
      logRenderSnapshotDebug({ event: 'queue_push', roomId: room.roomId, revision: nextRevision, queueSize: queue.length, status: game.status, dtFromPrevMs: previousEntry ? Math.round(nextServerAt - previousEntry.serverAt) : null });
      debugState.lastLoggedAt = debugNow;
      debugState.lastQueueSize = queue.length;
      debugState.lastMode = 'queue_push';
    }

    if (queue.length > REALTIME_SNAPSHOT_QUEUE_LIMIT) {
      queue.splice(0, queue.length - REALTIME_SNAPSHOT_QUEUE_LIMIT);
    }
  }, [animating, displayBalls, game.balls, game.snapshotRevision, game.status]);

  // Shot animation trigger
  useEffect(() => {
    if (animating) return;
    if (!game.lastShot || !game.lastShot.frames.length) return;
    if (game.lastShot.seq <= lastAnimatedSeqRef.current) return;
    playbackSettlingRef.current = false;
    const trimmedFrames = trimPlaybackFrames(game.lastShot.frames);
    playbackRef.current = {
      seq: game.lastShot.seq,
      frames: trimmedFrames,
      startedAt: performance.now(),
      baseBalls: game.balls,
      finalBalls: game.balls,
    };
    setAnimating(true);
    setAnimatingSeq(game.lastShot.seq);
    playbackSoundStateRef.current = { seq: game.lastShot.seq, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0, movingIds: new Set(), wallIds: new Set() };
  }, [animating, game]);

  useEffect(() => {
    const pending = pendingShotVisualRef.current;
    if (!pending) return;
    if (animating) {
      pendingShotVisualRef.current = null;
      return;
    }
    const progress = computePendingShotProgress(pending, performance.now());
    const absoluteExpiryMs = PENDING_SHOT_VISUAL_MAX_MS + PENDING_SHOT_POST_IMPACT_HOLD_MS + 260;
    if (progress.elapsed >= absoluteExpiryMs && game.status !== "simulating") {
      pendingShotVisualRef.current = null;
      return;
    }
    if (game.status !== "simulating") return;
    const authoritativeCueBall = game.balls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
    if (!authoritativeCueBall) return;
    const revision = game.snapshotRevision ?? 0;
    const shotSequenceAdvanced = game.shotSequence > pending.shotSequenceAtDispatch;
    const revisionAdvanced = revision > pending.revisionAtDispatch + 1;
    const authoritativeDistanceFromStart = Math.hypot(authoritativeCueBall.x - pending.cueX, authoritativeCueBall.y - pending.cueY);
    const authoritativeDistanceFromExpected = Math.hypot(authoritativeCueBall.x - progress.expectedCueX, authoritativeCueBall.y - progress.expectedCueY);
    const authoritativeNearExpected = authoritativeDistanceFromExpected <= Math.max(14, BALL_RADIUS * 1.35);
    const authoritativeClearlyStarted = authoritativeDistanceFromStart > Math.max(2.2, BALL_RADIUS * 0.4);
    const authoritativeMadeMeaningfulProgress = authoritativeDistanceFromStart > Math.max(9, pending.travelLimit * 0.22);
    const hardExpiryDuringSimMs = absoluteExpiryMs + 360;
    if (authoritativeNearExpected || (authoritativeClearlyStarted && (authoritativeMadeMeaningfulProgress || revisionAdvanced || shotSequenceAdvanced)) || progress.elapsed >= hardExpiryDuringSimMs) {
      pendingShotVisualRef.current = null;
    }
  }, [animating, game.balls, game.snapshotRevision, game.shotSequence, game.status]);

  useEffect(() => () => {
    if (drawLoopRef.current !== null) window.cancelAnimationFrame(drawLoopRef.current);
    if (powerReturnAnimRef.current !== null) window.cancelAnimationFrame(powerReturnAnimRef.current);
    railTimersRef.current.forEach((timer) => window.clearTimeout(timer));
    railTimersRef.current = [];
    clearQueuedSfx(queuedSfxRef);
    onAimStateChangeRef.current?.({ visible: false, angle: aimAngleRef.current, cueX: null, cueY: null, mode: "idle" });
  }, []);

  useEffect(() => {
    const settled = buildSettledRailBalls(game.balls);
    const nextCaptured = new Set(settled.map((ball) => ball.id));
    // Only add ALREADY-hidden balls to nextHidden. Newly pocketed balls should NOT
    // be hidden here — the render loop needs to see them to trigger pocket animation.
    const nextHidden = new Set<string>();
    for (const ball of game.balls) {
      if (ball.pocketed && hiddenPocketIdsRef.current.has(ball.id)) {
        nextHidden.add(ball.id);
      }
    }
    const differs = settled.length !== railBalls.length || settled.some((ball, index) => {
      const current = railBalls[index];
      return !current || current.id !== ball.id || current.number !== ball.number || current.state !== "settled";
    });
    const shouldReset = game.shotSequence === 0 || game.phase === "break";
    if (shouldReset && differs) {
      railCapturedIdsRef.current = nextCaptured;
      hiddenPocketIdsRef.current = nextHidden;
      setRailBalls(settled);
      return;
    }
    if (settled.length > railBalls.length && railBalls.every((ball) => ball.state === "settled")) {
      railCapturedIdsRef.current = nextCaptured;
      hiddenPocketIdsRef.current = nextHidden;
      setRailBalls(settled);
    }
  }, [game.balls, game.gameId, game.phase, game.shotSequence, railBalls]);

  useEffect(() => {
    const authoritativeCue = game.balls.find((ball) => ball.number === 0) ?? null;
    if (authoritativeCue && !authoritativeCue.pocketed) {
      hiddenPocketIdsRef.current.delete(authoritativeCue.id);
      cuePocketHoldUntilRef.current = 0;
      return;
    }
    if (game.ballInHandUserId && performance.now() >= cuePocketHoldUntilRef.current && authoritativeCue?.id) {
      hiddenPocketIdsRef.current.delete(authoritativeCue.id);
    }
  }, [game.ballInHandUserId, game.balls]);

  const capturePocketedBall = (ball: GameBallSnapshot, now: number) => {
    if (hiddenPocketIdsRef.current.has(ball.id)) return;
    hiddenPocketIdsRef.current.add(ball.id);
    if (ball.number === 0) {
      cuePocketHoldUntilRef.current = now + POCKET_ANIM_DURATION + CUE_RETURN_HOLD_MS;
      return;
    }
    if (railCapturedIdsRef.current.has(ball.id)) return;
    railCapturedIdsRef.current.add(ball.id);
    let nextSlot = 0;
    setRailBalls((current) => {
      nextSlot = current.length;
      return [...current, { id: ball.id, number: ball.number, lane: nextSlot % 3, slot: nextSlot, state: "travel" }];
    });
    const settleTimer = window.setTimeout(() => {
      setRailBalls((current) => current.map((entry) => (entry.id === ball.id ? { ...entry, state: "settled" } : entry)));
    }, RAIL_TRAVEL_DURATION_MS + RAIL_SETTLE_DELAY_MS);
    railTimersRef.current.push(settleTimer);
  };

  const renderBalls = useMemo(() => {
    const visibleNonCue = displayBalls.filter((ball) => !ball.pocketed && ball.number !== 0);
    if ((game.phase === "break" || game.shotSequence === 0) && visibleNonCue.length < 15) {
      return buildOpeningBalls(displayBalls);
    }
    if (game.ballInHandUserId) {
      const cue = displayBalls.find((ball) => ball.number === 0) ?? null;
      const holdCueReturn = performance.now() < cuePocketHoldUntilRef.current;
      if ((!cue || cue.pocketed) && !holdCueReturn) {
        const placed = clampCuePosition(DEFAULT_CUE_X, DEFAULT_CUE_Y, game.phase === "break");
        return [
          { id: cue?.id ?? "ball-0", number: 0, x: placed.x, y: placed.y, pocketed: false },
          ...displayBalls.filter((ball) => ball.number !== 0),
        ];
      }
    }
    return displayBalls;
  }, [displayBalls, game.ballInHandUserId, game.phase, game.shotSequence]);

  const cueBall = renderBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const canInteract = Boolean(cueBall && isMyTurn && !animating && !shootBusy && game.status === "waiting_shot");
  const isBallInHand = game.ballInHandUserId === currentUserId && canInteract;
  canInteractRef.current = canInteract;
  shootBusyRef.current = shootBusy;
  onShootRef.current = onShoot;
  const cueLabel = game.tableType === "casual" ? "amistoso" : `${game.stakeChips ?? 0}`;
  const myRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === myGroup).length;
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;
  const leftPocketed = useMemo(() => pocketedNumbersForGroup(game.balls, leftGroup), [game.balls, leftGroup]);
  const rightPocketed = useMemo(() => pocketedNumbersForGroup(game.balls, rightGroup), [game.balls, rightGroup]);

  useEffect(() => {
    if (!cueBall) return;
    if (game.phase === "break") {
      setAimAngle(0); aimAngleRef.current = 0; drawAimAngleRef.current = 0;
      return;
    }
    if (game.turnUserId !== currentUserId) {
      setAimAngle(Math.PI); aimAngleRef.current = Math.PI; drawAimAngleRef.current = Math.PI;
    }
  }, [cueBall?.id, cueBall?.x, cueBall?.y, currentUserId, game.phase, game.turnUserId]);

  useEffect(() => { if (!needEightCall) setSelectedPocket(null); }, [needEightCall]);

  // #16: Show group assignment banner only after the scoring shot has visually ended.
  const prevGroupRef = useRef<BallGroup | null>(myGroup);
  useEffect(() => {
    const previous = prevGroupRef.current;
    prevGroupRef.current = myGroup;
    if (!myGroup || previous) return;
    const label = myGroup === "solids" ? "Suas bolas são lisas!" : "Suas bolas são listradas!";
    setGroupBanner(label);
    const timer = window.setTimeout(() => setGroupBanner(null), 2500);
    return () => window.clearTimeout(timer);
  }, [myGroup]);
  useEffect(() => { powerRef.current = power; }, [power]);
  useEffect(() => {
    if (game.ballInHandUserId === currentUserId) {
      if (!localCuePlacementRef.current) {
        const cue = game.balls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
        const placed = clampCuePosition(cue?.x ?? DEFAULT_CUE_X, cue?.y ?? DEFAULT_CUE_Y, game.shotSequence === 0);
        localCuePlacementRef.current = placed;
      }
    } else {
      localCuePlacementRef.current = null;
    }
    if (!shootBusy && pointerModeRef.current !== "power" && powerRef.current !== POWER_MIN) {
      powerRef.current = POWER_MIN;
      setPower(POWER_MIN);
    }
  }, [currentUserId, game.ballInHandUserId, game.balls, game.shotSequence, shootBusy]);
  useEffect(() => {
    pointerModeRef.current = pointerMode;
    if (pointerMode !== "power") powerPointerIdRef.current = null;
  }, [pointerMode]);

  const isCueBallPlacementActive = isBallInHand && pointerMode === "place";
  const turnControlVisible = Boolean(cueBall && game.status !== "finished" && !animating && isMyTurn && !isCueBallPlacementActive);
  const powerBarInteractive = canInteract && !isCueBallPlacementActive;
  const powerVisual = clamp((power - POWER_MIN) / (1 - POWER_MIN), 0, 1);
  const displayedPowerVisual = isCueBallPlacementActive ? 0.08 : powerVisual;
  const displayedPowerCueTop = clamp(displayedPowerVisual, 0, 0.935);
  const browserSupportsPointerEvents = typeof window !== "undefined" && "PointerEvent" in window;

  const emitAimState = (next: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; mode: AimPointerMode }, force = false) => {
    const handler = onAimStateChangeRef.current;
    if (!handler) return;
    const payload = {
      visible: next.visible,
      angle: Number.isFinite(next.angle) ? next.angle : 0,
      cueX: next.cueX ?? null,
      cueY: next.cueY ?? null,
      power: clamp(next.power ?? powerRef.current ?? POWER_MIN, 0, 1),
      seq: force ? aimSeqRef.current + 1 : aimSeqRef.current + 1,
      mode: next.mode,
    };
    const key = `${payload.visible ? 1 : 0}:${payload.mode}:${payload.angle.toFixed(3)}:${payload.cueX === null ? "n" : payload.cueX.toFixed(1)}:${payload.cueY === null ? "n" : payload.cueY.toFixed(1)}:${payload.power.toFixed(3)}`;
    const now = performance.now();
    const minInterval = payload.mode === "place" ? PLACE_SYNC_INTERVAL_MS : AIM_SYNC_INTERVAL_MS;
    if (!force && key === lastAimPayloadKeyRef.current && now - lastAimEmitAtRef.current < minInterval) return;
    if (!force && payload.visible && now - lastAimEmitAtRef.current < minInterval) return;
    aimSeqRef.current = payload.seq;
    lastAimPayloadKeyRef.current = key;
    lastAimEmitAtRef.current = now;
    handler(payload);
  };

  useEffect(() => {
    if (pointerMode === "place") {
      emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "place" }, true);
      return;
    }
    if (!canInteract || animating || shootBusy || game.status !== "waiting_shot") {
      emitAimState({ visible: false, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "idle" }, true);
    }
  }, [animating, canInteract, cueBall?.x, cueBall?.y, game.status, pointerMode, shootBusy]);

  const setPointerModeSafe = (next: PointerMode) => { pointerModeRef.current = next; setPointerMode(next); };

  const mapPowerFromClientY = (clientY: number, rectTop: number, rectBottom: number, rectHeight: number) => {
    const clampedY = clamp(clientY, rectTop, rectBottom);
    const normalized = clamp((clampedY - rectTop) / Math.max(1, rectHeight), 0, 1);
    let shaped = 0;
    if (normalized <= 0.36) {
      shaped = Math.pow(normalized / 0.36, POWER_CURVE_EXPONENT) * 0.16;
    } else if (normalized <= 0.82) {
      shaped = 0.16 + Math.pow((normalized - 0.36) / 0.46, 1.62) * 0.36;
    } else {
      shaped = 0.52 + Math.pow((normalized - 0.82) / 0.18, 1.95) * 0.48;
    }
    return clamp(POWER_MIN + shaped * (1 - POWER_MIN), POWER_MIN, 1);
  };

  const pointToLocal = (clientX: number, clientY: number) => {
    if (!tableWrapRef.current) return null;
    const rect = tableWrapRef.current.getBoundingClientRect();
    return { x: ((clientX - rect.left) / rect.width) * TABLE_WIDTH, y: ((clientY - rect.top) / rect.height) * TABLE_HEIGHT };
  };

  const beginAimDrag = (point: LocalPoint) => {
    aimDragRef.current = { x: point.x, y: point.y };
    if (cueBall) {
      emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall.x, cueY: cueBall.y, mode: "aim" }, true);
    }
  };

  const updateAimFromDrag = (point: LocalPoint) => {
    if (!cueBall) return;
    const previous = aimDragRef.current;
    if (!previous) {
      aimDragRef.current = { x: point.x, y: point.y };
      emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall.x, cueY: cueBall.y, mode: "aim" });
      return;
    }

    const prevDx = previous.x - cueBall.x;
    const prevDy = previous.y - cueBall.y;
    const currDx = point.x - cueBall.x;
    const currDy = point.y - cueBall.y;
    const prevDistance = Math.hypot(prevDx, prevDy);
    const currDistance = Math.hypot(currDx, currDy);
    const orbitThreshold = BALL_RADIUS * 2.35;

    aimDragRef.current = { x: point.x, y: point.y };
    if (prevDistance < orbitThreshold || currDistance < orbitThreshold) return;

    const prevAngle = Math.atan2(prevDy, prevDx);
    const currAngle = Math.atan2(currDy, currDx);
    const rawDelta = Math.atan2(
      Math.sin(currAngle - prevAngle),
      Math.cos(currAngle - prevAngle),
    );

    const averageRadius = (prevDistance + currDistance) * 0.5;
    const arcPixels = Math.abs(rawDelta) * averageRadius;
    if (arcPixels < 1.15) return;

    const gain = averageRadius < 120 ? 0.9 : averageRadius < 240 ? 0.95 : 1.0;
    const maxStep = averageRadius < 120 ? 0.04 : averageRadius < 240 ? 0.055 : 0.075;
    const delta = clamp(rawDelta * gain, -maxStep, maxStep);
    if (Math.abs(delta) < 0.0009) return;

    aimAngleRef.current += delta;
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall.x, cueY: cueBall.y, mode: "aim" });
  };

  const updateCuePositionFromPoint = (point: LocalPoint) => {
    if (!cueBall) return;
    const next = clampCuePosition(point.x, point.y, game.shotSequence === 0);
    localCuePlacementRef.current = next;
    setDisplayBalls((current) => current.map((ball) => (ball.number === 0 ? { ...ball, x: next.x, y: next.y, pocketed: false } : ball)));
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: next.x, cueY: next.y, mode: "place" }, true);
  };

  const updatePowerFromClientY = (clientY: number) => {
    const gesture = powerGestureRef.current;
    if (!gesture) return;
    const next = mapPowerFromClientY(clientY, gesture.rectTop, gesture.rectBottom, gesture.rectHeight);
    powerRef.current = next;
    setPower(next);
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, power: next, mode: "power" });
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    try { event.currentTarget.setPointerCapture?.(event.pointerId); } catch {}
    pointerMovedRef.current = false;
    SFX.prime();
    if (isBallInHand && pointInCircle(point, cueBall.x, cueBall.y, BALL_RADIUS * 2.2)) {
      setPointerModeSafe("place");
      updateCuePositionFromPoint(point);
      return;
    }
    setPointerModeSafe("aim");
    beginAimDrag(point);
    pointerMovedRef.current = false;
  };

  const handleTablePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    if (pointerMode === "place") { updateCuePositionFromPoint(point); return; }
    if (pointerMode === "aim") { updateAimFromDrag(point); pointerMovedRef.current = true; }
  };

  const handleTablePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    aimDragRef.current = null;
    const previousMode = pointerModeRef.current;
    const latestCueBall = renderStateRef.current.cueBall;
    const placedCue = previousMode === "place" ? localCuePlacementRef.current : null;
    emitAimState({
      visible: false,
      angle: aimAngleRef.current,
      cueX: placedCue?.x ?? latestCueBall?.x ?? cueBall?.x ?? null,
      cueY: placedCue?.y ?? latestCueBall?.y ?? cueBall?.y ?? null,
      mode: "idle",
    }, true);
    setPointerModeSafe("idle");
  };

  const animatePowerReturn = (from: number) => {
    if (powerReturnAnimRef.current !== null) {
      window.cancelAnimationFrame(powerReturnAnimRef.current);
      powerReturnAnimRef.current = null;
    }
    const start = performance.now();
    const step = (now: number) => {
      const t = clamp((now - start) / POWER_RETURN_MS, 0, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      const next = lerp(from, POWER_MIN, eased);
      powerRef.current = next;
      setPower(next);
      if (t < 1) powerReturnAnimRef.current = window.requestAnimationFrame(step);
      else powerReturnAnimRef.current = null;
    };
    powerReturnAnimRef.current = window.requestAnimationFrame(step);
  };

  const commitPowerShot = () => {
    if (pointerModeRef.current !== "power" || powerReleaseGuardRef.current) return;
    powerReleaseGuardRef.current = true;
    powerGestureRef.current = null;
    const state = renderStateRef.current;
    const liveCueBall = state.cueBall;
    const shotPower = clamp(powerRef.current, POWER_MIN, 1);
    const shotStartedAt = performance.now();
    snapAnimRef.current = { startedAt: shotStartedAt, power: shotPower, fired: true };
    clearQueuedSfx(queuedSfxRef);
    SFX.prime();
    pendingShotVisualRef.current = null;
    if (liveCueBall) {
      const livePreview = computeAimPreview(liveCueBall, state.renderBalls, aimAngleRef.current);
      const travelLimit = livePreview.contactX !== null && livePreview.contactY !== null
        ? Math.max(20, Math.hypot(livePreview.contactX - liveCueBall.x, livePreview.contactY - liveCueBall.y))
        : Math.max(24, Math.hypot(livePreview.endX - liveCueBall.x, livePreview.endY - liveCueBall.y));
      const estimatedSpeedPxPerMs = lerp(0.95, 4.85, Math.pow(shotPower, 0.58));
      const impactAtMs = travelLimit > 1 ? clamp(travelLimit / estimatedSpeedPxPerMs, 18, 210) : null;
      const impactType: "ball" | "cushion" | null = livePreview.hitBall ? "ball" : "cushion";
      pendingShotVisualRef.current = {
        startedAt: shotStartedAt,
        shotSequenceAtDispatch: game.shotSequence,
        revisionAtDispatch: game.snapshotRevision ?? 0,
        angle: aimAngleRef.current,
        power: shotPower,
        cueX: liveCueBall.x,
        cueY: liveCueBall.y,
        travelLimit,
        estimatedSpeedPxPerMs,
        impactType,
        impactAtMs,
        firstImpactPlayed: false,
      };
      SFX.cueHit(shotPower);
      if (impactAtMs !== null && impactType) {
        queueSfx(queuedSfxRef, impactAtMs, () => {
          if (impactType === "ball") SFX.ballHit();
          else if (impactType === "cushion") SFX.cushion();
        });
      }
    }
    setPointerModeSafe("idle");
    emitAimState({ visible: false, angle: aimAngleRef.current, cueX: liveCueBall?.x ?? null, cueY: liveCueBall?.y ?? null, mode: "idle" }, true);

    if (!liveCueBall || !canInteractRef.current || shootBusyRef.current) {
      const reason = !liveCueBall ? 'missing_live_cue_ball' : (!canInteractRef.current ? 'cannot_interact' : 'shoot_busy');
      onShotDebugEvent?.({
        stage: 'ui_commit_blocked',
        roomId: room.roomId,
        angle: aimAngleRef.current,
        power: shotPower,
        cueX: liveCueBall?.x ?? null,
        cueY: liveCueBall?.y ?? null,
        reason,
        note: `commit bloqueado: ${reason}`,
      });
      animatePowerReturn(shotPower);
      return;
    }

    const payload = {
      angle: aimAngleRef.current,
      power: shotPower,
      cueX: state.isBallInHand ? liveCueBall.x : null,
      cueY: state.isBallInHand ? liveCueBall.y : null,
      calledPocket: state.needEightCall ? state.selectedPocket : null,
      spinX: 0,
      spinY: 0,
    };

    console.log("[sinuca-shot-ui-commit]", JSON.stringify({
      shotPower,
      angle: aimAngleRef.current,
      cueX: state.isBallInHand ? liveCueBall.x : null,
      cueY: state.isBallInHand ? liveCueBall.y : null,
      isBallInHand: state.isBallInHand,
      needEightCall: state.needEightCall,
      selectedPocket: state.selectedPocket ?? null,
      canInteract: canInteractRef.current,
      shootBusy: shootBusyRef.current,
      gameStatus: game.status,
      shotSequence: game.shotSequence,
      turnUserId: game.turnUserId,
    }));
    onShotDebugEvent?.({
      stage: 'ui_commit',
      roomId: room.roomId,
      angle: aimAngleRef.current,
      power: shotPower,
      cueX: state.isBallInHand ? liveCueBall.x : null,
      cueY: state.isBallInHand ? liveCueBall.y : null,
      note: state.isBallInHand ? 'ui_commit com ball in hand' : 'ui_commit normal',
    });

    if (state.isBallInHand) {
      localCuePlacementRef.current = { x: liveCueBall.x, y: liveCueBall.y };
    }

    // Fire the real shot immediately, then let visual/audio reactions happen in parallel.
    void onShootRef.current(payload).catch((error) => {
      onShotDebugEvent?.({
        stage: 'ui_commit_error',
        roomId: room.roomId,
        angle: payload.angle,
        power: payload.power,
        cueX: payload.cueX ?? null,
        cueY: payload.cueY ?? null,
        reason: 'ui_commit_error',
        note: error instanceof Error ? error.message : String(error),
      });
      console.warn("[sinuca-shot-ui-error]", JSON.stringify({ message: error instanceof Error ? error.message : String(error) }));
    });
    animatePowerReturn(shotPower);
      };

  const startPowerGesture = (clientY: number) => {
    if (!canInteractRef.current || !powerRailRef.current) return false;
    SFX.prime();
    if (powerReturnAnimRef.current !== null) {
      window.cancelAnimationFrame(powerReturnAnimRef.current);
      powerReturnAnimRef.current = null;
    }
    const rect = (powerTrackRef.current ?? powerRailRef.current).getBoundingClientRect();
    powerGestureRef.current = {
      rectTop: rect.top + 2,
      rectBottom: rect.bottom - 2,
      rectHeight: Math.max(1, rect.height - 4),
    };
    powerReleaseGuardRef.current = false;
    setPointerModeSafe("power");
    updatePowerFromClientY(clientY);
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, power: powerRef.current, mode: "power" }, true);
    return true;
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    powerTouchIdRef.current = null;
    powerPointerIdRef.current = event.pointerId;
    if (!startPowerGesture(event.clientY)) {
      powerPointerIdRef.current = null;
      return;
    }
    try { event.currentTarget.setPointerCapture?.(event.pointerId); } catch {}
  };

  const handlePowerTouchStart = (event: ReactTouchEvent<HTMLDivElement>) => {
    if (browserSupportsPointerEvents) return;
    const touch = event.changedTouches[0];
    if (!touch) return;
    event.preventDefault();
    event.stopPropagation();
    powerPointerIdRef.current = null;
    powerTouchIdRef.current = touch.identifier;
    if (!startPowerGesture(touch.clientY)) {
      powerTouchIdRef.current = null;
    }
  };

  const handlePowerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerModeRef.current !== "power") return;
    if (powerPointerIdRef.current !== null && event.pointerId !== powerPointerIdRef.current) return;
    event.preventDefault();
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerTouchMove = (event: ReactTouchEvent<HTMLDivElement>) => {
    if (browserSupportsPointerEvents || pointerModeRef.current !== "power") return;
    const identifier = powerTouchIdRef.current;
    const touch = findTouchById(event.changedTouches, identifier);
    if (!touch) return;
    event.preventDefault();
    updatePowerFromClientY(touch.clientY);
  };

  const handlePowerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (powerPointerIdRef.current !== null && event.pointerId !== powerPointerIdRef.current) return;
    try { event.currentTarget.releasePointerCapture?.(event.pointerId); } catch {}
    powerPointerIdRef.current = null;
    powerTouchIdRef.current = null;
    commitPowerShot();
  };

  const handlePowerTouchEnd = (event: ReactTouchEvent<HTMLDivElement>) => {
    if (browserSupportsPointerEvents || pointerModeRef.current !== "power") return;
    const identifier = powerTouchIdRef.current;
    const touch = findTouchById(event.changedTouches, identifier);
    if (!touch && identifier !== null) return;
    event.preventDefault();
    powerTouchIdRef.current = null;
    powerPointerIdRef.current = null;
    commitPowerShot();
  };

  const handlePowerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (powerPointerIdRef.current !== null && event.pointerId !== powerPointerIdRef.current) return;
    try { event.currentTarget.releasePointerCapture?.(event.pointerId); } catch {}
    powerPointerIdRef.current = null;
    powerTouchIdRef.current = null;
    if (pointerModeRef.current === "power" && !powerReleaseGuardRef.current) commitPowerShot();
  };

  const handlePowerTouchCancel = (event: ReactTouchEvent<HTMLDivElement>) => {
    if (browserSupportsPointerEvents || pointerModeRef.current !== "power") return;
    event.preventDefault();
    powerTouchIdRef.current = null;
    powerPointerIdRef.current = null;
    if (!powerReleaseGuardRef.current) commitPowerShot();
  };

  const handlePowerLostCapture = () => {
    // Discord Activity/webview can drop pointer capture mid-gesture.
    // Keep the gesture alive and let the global listeners finish the shot.
  };

  useEffect(() => {
    if (pointerMode !== "power") return;
    const handleWindowMove = (event: PointerEvent) => {
      if (pointerModeRef.current !== "power") return;
      if (powerPointerIdRef.current !== null && event.pointerId !== powerPointerIdRef.current) return;
      updatePowerFromClientY(event.clientY);
    };
    const finishFromWindow = (event: PointerEvent) => {
      if (pointerModeRef.current !== "power") return;
      if (powerPointerIdRef.current !== null && event.pointerId !== powerPointerIdRef.current) return;
      powerPointerIdRef.current = null;
      powerTouchIdRef.current = null;
      commitPowerShot();
    };
    const handleTouchMove = (event: TouchEvent) => {
      if (pointerModeRef.current !== "power") return;
      const identifier = powerTouchIdRef.current;
      const touch = findTouchById(event.changedTouches, identifier)
        ?? findTouchById(event.touches, identifier)
        ?? null;
      if (!touch) return;
      event.preventDefault();
      updatePowerFromClientY(touch.clientY);
    };
    const finishFromTouch = (event: TouchEvent) => {
      if (pointerModeRef.current !== "power") return;
      const identifier = powerTouchIdRef.current;
      const touch = findTouchById(event.changedTouches, identifier);
      if (!touch && identifier !== null) return;
      event.preventDefault();
      powerPointerIdRef.current = null;
      powerTouchIdRef.current = null;
      commitPowerShot();
    };
    window.addEventListener("pointermove", handleWindowMove);
    window.addEventListener("pointerup", finishFromWindow);
    window.addEventListener("pointercancel", finishFromWindow);
    if (!browserSupportsPointerEvents) {
      window.addEventListener("touchmove", handleTouchMove, { passive: false });
      window.addEventListener("touchend", finishFromTouch, { passive: false });
      window.addEventListener("touchcancel", finishFromTouch, { passive: false });
    }
    return () => {
      window.removeEventListener("pointermove", handleWindowMove);
      window.removeEventListener("pointerup", finishFromWindow);
      window.removeEventListener("pointercancel", finishFromWindow);
      if (!browserSupportsPointerEvents) {
        window.removeEventListener("touchmove", handleTouchMove);
        window.removeEventListener("touchend", finishFromTouch);
        window.removeEventListener("touchcancel", finishFromTouch);
      }
    };
  }, [pointerMode, room.roomId, currentUserId, shootBusy, animating, canInteract, isBallInHand, needEightCall, selectedPocket, browserSupportsPointerEvents]);

  const statusText = game.status === "finished"
    ? game.winnerUserId === currentUserId ? "Você venceu" : "Você perdeu"
    : game.status === "simulating"
      ? "Tacada em andamento"
      : game.foulReason && !animating
        ? `Falta: ${game.foulReason}`
        : isBallInHand
          ? "Bola na mão"
          : animating
            ? `Tacada ${animatingSeq}`
            : isMyTurn
              ? needEightCall ? "Escolha a caçapa" : "Sua vez"
              : `Vez de ${cleanName(opponent?.displayName ?? "oponente")}`;

  const phaseText = game.status === "finished"
    ? "fim"
    : game.phase === "break" ? "break"
    : game.phase === "open_table" ? "mesa aberta"
    : game.phase === "eight_ball" ? "bola 8"
    : isOpenTable ? "pool"
    : myGroup === "solids" ? "lisas" : "listradas";

  useEffect(() => {
    renderStateRef.current = {
      renderBalls, cueBall, canInteract, pointerMode, power,
      needEightCall: needEightCall && isMyTurn, selectedPocket, isBallInHand, shootBusy,
    };
  }, [canInteract, cueBall, isBallInHand, isMyTurn, needEightCall, pointerMode, power, renderBalls, selectedPocket, shootBusy]);

  // ─── Render loop (optimized for smoothness) ─────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    const draw = () => {
      const now = performance.now();
      // Use moderate DPR cap for smooth performance
      const dpr = Math.min(1.5, Math.max(1, window.devicePixelRatio || 1));
      const targetWidth = Math.round(TABLE_WIDTH * dpr);
      const targetHeight = Math.round(TABLE_HEIGHT * dpr);
      if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
      }
      context.setTransform(dpr, 0, 0, dpr, 0, 0);

      const state = renderStateRef.current;
      const targetAngle = aimAngleRef.current;
      // Keep the cue following the aim direction smoothly without feeling detached.
      const aimLerp = state.pointerMode === "aim" ? 0.66 : state.pointerMode === "power" ? 0.52 : 0.22;
      drawAimAngleRef.current = lerpAngle(drawAimAngleRef.current, targetAngle, aimLerp);

      let drawBalls = state.renderBalls;
      let drawCueBall = state.cueBall;
      const playback = playbackRef.current;

      if (playback && playback.frames.length) {
        const frameStepMs = 1000 / 60;
        const elapsed = now - playback.startedAt;
        const nominalDuration = Math.max(frameStepMs, (playback.frames.length - 1) * frameStepMs * 0.94);
        const playbackDuration = Math.min(MAX_PLAYBACK_DURATION_MS, nominalDuration);
        const progress = clamp(elapsed / playbackDuration, 0, 1);
        const rawIndex = progress * Math.max(0, playback.frames.length - 1);
        const frameIndex = Math.min(playback.frames.length - 1, Math.floor(rawIndex));
        const settlePlayback = () => {
          if (playbackSettlingRef.current) return;
          playbackSettlingRef.current = true;
          // Pre-sync visual refs to final positions BEFORE the state update fires,
          // so there's no position jump that would cause micro-flicks or spin spikes
          const lastFrameBalls = frameToDisplayBalls(playback.frames[playback.frames.length - 1].balls, playback.baseBalls);
          realtimeVisualBallsRef.current = lastFrameBalls.map((ball) => ({ ...ball }));
          realtimeVisualLastAtRef.current = now;
          // Also sync spin cache lastX/lastY to final positions
          for (const ball of lastFrameBalls) {
            const spin = ballSpinRef.current.get(ball.id);
            if (spin) { spin.lastX = ball.x; spin.lastY = ball.y; spin.lastSeenAt = now; }
          }
          window.setTimeout(() => {
            if (playbackRef.current?.seq !== playback.seq) return;
            lastAnimatedSeqRef.current = playback.seq;
            playbackRef.current = null;
            setDisplayBalls(playback.finalBalls);
            setAnimating(false);
            setAnimatingSeq(0);
          }, 0);
        };

        if (progress >= 1 || frameIndex >= playback.frames.length - 1) {
          drawBalls = frameToDisplayBalls(playback.frames[playback.frames.length - 1].balls, playback.baseBalls);
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
          settlePlayback();
        } else {
          const frameA = playback.frames[frameIndex];
          const frameB = playback.frames[Math.min(playback.frames.length - 1, frameIndex + 1)];
          const localT = clamp(rawIndex - frameIndex, 0, 1);
          drawBalls = interpolateFrameBalls(playback.baseBalls, frameA.balls, frameB.balls, localT);
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
          const nearFinalFrame = frameIndex >= Math.max(0, playback.frames.length - 4);
          if (nearFinalFrame && ballsNearlyMatchFrame(drawBalls, playback.frames[playback.frames.length - 1].balls, 0.5)) {
            settlePlayback();
          }
        }
      }

      if ((!playback || !playback.frames.length) && game.status === "simulating" && drawBalls.length) {
        const queue = snapshotQueueRef.current;
        const latestSnapshot = queue[queue.length - 1] ?? null;
        const estimatedServerNow = latestSnapshot
          ? latestSnapshot.serverAt + Math.max(0, now - latestSnapshot.receivedAt)
          : Date.now();
        const renderServerTime = estimatedServerNow - REALTIME_RENDER_DELAY_MS;
        if (queue.length >= 2) {
          while (queue.length >= 3 && queue[1].serverAt <= renderServerTime - 2) {
            queue.shift();
          }
          const fromSnapshot = queue[0];
          const toSnapshot = queue[1] ?? queue[0];
          const span = Math.max(1, toSnapshot.serverAt - fromSnapshot.serverAt);
          const rawT = (renderServerTime - fromSnapshot.serverAt) / span;
          let smoothedBalls: GameBallSnapshot[];
          if (rawT <= 1) {
            const eased = clamp(rawT, 0, 1);
            const hermiteBalls = interpolateSnapshotEntries(fromSnapshot, toSnapshot, eased);
            const carry = clamp(1 - Math.exp(-(now - realtimeVisualLastAtRef.current) / 18), 0.42, 0.82);
            smoothedBalls = interpolateSnapshotBalls(realtimeVisualBallsRef.current, hermiteBalls, carry);
            // Snap nearly-stopped balls to target to eliminate micro-flick jitter
            for (let i = 0; i < smoothedBalls.length; i++) {
              const target = hermiteBalls[i];
              if (!target) continue;
              const dist = Math.hypot(smoothedBalls[i].x - target.x, smoothedBalls[i].y - target.y);
              if (dist < 1.0) { smoothedBalls[i] = { ...smoothedBalls[i], x: target.x, y: target.y }; }
            }
          } else {
            const overshootMs = Math.max(0, renderServerTime - toSnapshot.serverAt);
            const extrapolated = extrapolateSnapshotBalls(toSnapshot.balls, toSnapshot.velocities, Math.min(overshootMs, REALTIME_MAX_EXTRAPOLATION_MS));
            const carry = clamp(1 - Math.exp(-(now - realtimeVisualLastAtRef.current) / 20), 0.38, 0.76);
            smoothedBalls = interpolateSnapshotBalls(realtimeVisualBallsRef.current, extrapolated, carry);
            for (let i = 0; i < smoothedBalls.length; i++) {
              const target = extrapolated[i];
              if (!target) continue;
              const dist = Math.hypot(smoothedBalls[i].x - target.x, smoothedBalls[i].y - target.y);
              if (dist < 1.0) { smoothedBalls[i] = { ...smoothedBalls[i], x: target.x, y: target.y }; }
            }
          }
          realtimeVisualBallsRef.current = smoothedBalls.map((ball) => ({ ...ball }));
          realtimeVisualLastAtRef.current = now;
          drawBalls = smoothedBalls;
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
          const debugState = snapshotRenderDebugRef.current;
          if (now - debugState.lastLoggedAt >= SNAPSHOT_RENDER_DEBUG_LOG_EVERY_MS || debugState.lastMode !== 'interpolate') {
            logRenderSnapshotDebug({ event: 'interpolate', roomId: room.roomId, queueSize: queue.length, fromRevision: fromSnapshot.revision, toRevision: toSnapshot.revision, rawT: Math.round(rawT * 1000) / 1000, renderLagMs: Math.round(estimatedServerNow - toSnapshot.serverAt) });
            debugState.lastLoggedAt = now;
            debugState.lastQueueSize = queue.length;
            debugState.lastMode = 'interpolate';
          }
        } else if (queue.length === 1) {
          const holdSnapshot = queue[0];
          const extrapolationMs = Math.max(0, renderServerTime - holdSnapshot.serverAt);
          const extrapolated = extrapolateSnapshotBalls(holdSnapshot.balls, holdSnapshot.velocities, Math.min(extrapolationMs, REALTIME_MAX_EXTRAPOLATION_MS));
          const carry = clamp(1 - Math.exp(-(now - realtimeVisualLastAtRef.current) / 22), 0.4, 0.78);
          const smoothedBalls = interpolateSnapshotBalls(realtimeVisualBallsRef.current, extrapolated, carry);
          for (let i = 0; i < smoothedBalls.length; i++) {
            const target = extrapolated[i];
            if (!target) continue;
            const dist = Math.hypot(smoothedBalls[i].x - target.x, smoothedBalls[i].y - target.y);
            if (dist < 1.0) { smoothedBalls[i] = { ...smoothedBalls[i], x: target.x, y: target.y }; }
          }
          realtimeVisualBallsRef.current = smoothedBalls.map((ball) => ({ ...ball }));
          realtimeVisualLastAtRef.current = now;
          drawBalls = smoothedBalls;
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
          const debugState = snapshotRenderDebugRef.current;
          if (now - debugState.lastLoggedAt >= SNAPSHOT_RENDER_DEBUG_LOG_EVERY_MS || debugState.lastMode !== 'single_hold') {
            logRenderSnapshotDebug({ event: 'single_hold', roomId: room.roomId, queueSize: queue.length, revision: holdSnapshot.revision, extrapolationMs: Math.round(extrapolationMs) });
            debugState.lastLoggedAt = now;
            debugState.lastQueueSize = queue.length;
            debugState.lastMode = 'single_hold';
          }
        }
      } else if (!playback || !playback.frames.length) {
        const copied = drawBalls.map((ball) => ({ ...ball }));
        realtimeVisualBallsRef.current = copied;
        realtimeVisualLastAtRef.current = now;
        snapshotQueueRef.current = [{
          balls: copied.map((ball) => ({ ...ball })),
          revision: game.snapshotRevision ?? 0,
          receivedAt: now,
          serverAt: game.updatedAt || Date.now(),
          velocities: {},
        }];
      }

      const latestAuthoritativeSnapshot = snapshotQueueRef.current[snapshotQueueRef.current.length - 1] ?? null;
      if (!playback && latestAuthoritativeSnapshot) {
        const soundSnapshot = realtimeSoundSnapshotRef.current;
        if (soundSnapshot.roomId !== room.roomId || latestAuthoritativeSnapshot.revision < soundSnapshot.revision) {
          realtimeSoundSnapshotRef.current = {
            roomId: room.roomId,
            revision: latestAuthoritativeSnapshot.revision,
            balls: latestAuthoritativeSnapshot.balls.map((ball) => ({ ...ball })),
          };
          realtimeSoundCooldownRef.current.roomId = room.roomId;
          realtimeSoundCooldownRef.current.movingIds = new Set();
          realtimeSoundCooldownRef.current.wallIds = new Set();
        } else if (latestAuthoritativeSnapshot.revision > soundSnapshot.revision) {
          emitRealtimeImpactSounds(soundSnapshot.balls, latestAuthoritativeSnapshot.balls, now, realtimeSoundCooldownRef.current);
          realtimeSoundSnapshotRef.current = {
            roomId: room.roomId,
            revision: latestAuthoritativeSnapshot.revision,
            balls: latestAuthoritativeSnapshot.balls.map((ball) => ({ ...ball })),
          };
        }
      }

      // Detect newly pocketed or captured-near-pocket balls → trigger animations and rail return
      const currentPocketedIds = new Set<string>();
      for (const ball of drawBalls) { if (ball.pocketed) currentPocketedIds.add(ball.id); }
      for (const ball of drawBalls) {
        if (hiddenPocketIdsRef.current.has(ball.id)) continue;
        // Protect cue ball from premature capture while pending shot visual is active
        if (ball.number === 0 && pendingShotVisualRef.current && !ball.pocketed) continue;
        let closestPocket: typeof POCKETS[number] = POCKETS[0];
        let minDist = Infinity;
        for (const pocket of POCKETS) {
          const d = Math.hypot(pocket.x - ball.x, pocket.y - ball.y);
          if (d < minDist) { minDist = d; closestPocket = pocket; }
        }
        const spin = ballSpinRef.current.get(ball.id);
        const movingFastEnough = (spin?.visualSpeed ?? 0) > 0.05;
        const shouldCapture = ball.pocketed || (minDist <= POCKET_CAPTURE_DISTANCE && (movingFastEnough || minDist <= BALL_RADIUS * 0.62));
        if (!shouldCapture) continue;
        pocketAnimationsRef.current.push({ ball: { ...ball, pocketed: false }, pocketX: closestPocket.x, pocketY: closestPocket.y, startedAt: now });
        capturePocketedBall(ball, now);
        const cooldown = realtimeSoundCooldownRef.current;
        if (now - cooldown.lastPocketAt > 90) {
          SFX.pocket();
          cooldown.lastPocketAt = now;
        }
      }
      prevPocketedIdsRef.current = currentPocketedIds;
      pocketAnimationsRef.current = pocketAnimationsRef.current.filter((anim) => now - anim.startedAt < POCKET_ANIM_DURATION);
      if (hiddenPocketIdsRef.current.size) {
        drawBalls = drawBalls.map((ball) => (hiddenPocketIdsRef.current.has(ball.id) ? { ...ball, pocketed: true } : ball));
        drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? drawCueBall;
      }

      if (playback && playback.frames.length) {
        const frameStepMs = 1000 / 60;
        const elapsed = now - playback.startedAt;
        const nominalDuration = Math.max(frameStepMs, (playback.frames.length - 1) * frameStepMs * 0.94);
        const playbackDuration = Math.min(MAX_PLAYBACK_DURATION_MS, nominalDuration);
        const progress = clamp(elapsed / playbackDuration, 0, 1);
        const rawIndex = progress * Math.max(0, playback.frames.length - 1);
        const soundFrameIndex = Math.min(playback.frames.length - 1, Math.floor(rawIndex));
        const soundState = playbackSoundStateRef.current;
        if (soundState.seq !== playback.seq) {
          playbackSoundStateRef.current = { seq: playback.seq, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0, movingIds: new Set(), wallIds: new Set() };
        }
        if (soundFrameIndex > playbackSoundStateRef.current.frameIndex) {
          const prevFrame = playback.frames[Math.max(0, soundFrameIndex - 1)]?.balls ?? [];
          const currFrame = playback.frames[soundFrameIndex]?.balls ?? [];
          const prevMap = new Map(prevFrame.map((ball) => [ball.id, ball]));
          let newlyPocketed = false;
          const movingIds = new Set<string>();
          const wallIds = new Set<string>();
          for (const ball of currFrame) {
            const prevBall = prevMap.get(ball.id);
            if (!prevBall) continue;
            if (ball.pocketed && !prevBall.pocketed) newlyPocketed = true;
            const moved = Math.hypot(ball.x - prevBall.x, ball.y - prevBall.y);
            if (!ball.pocketed && ball.id !== "ball-0" && moved > 0.8) movingIds.add(ball.id);
            if (!ball.pocketed && moved > 0.6 && (ball.x <= PLAY_MIN_X + 2.4 || ball.x >= PLAY_MAX_X - 2.4 || ball.y <= PLAY_MIN_Y + 2.4 || ball.y >= PLAY_MAX_Y - 2.4)) {
              wallIds.add(ball.id);
            }
          }
          const stateNow = playbackSoundStateRef.current;
          const newlyMoving = Array.from(movingIds).filter((id) => !stateNow.movingIds.has(id));
          const newlyWall = Array.from(wallIds).filter((id) => !stateNow.wallIds.has(id));
          if (newlyMoving.length > 0 && now - stateNow.lastBallAt > 160) {
            SFX.ballHit();
            stateNow.lastBallAt = now;
          } else if (newlyWall.length > 0 && now - stateNow.lastCushionAt > 140 && !newlyPocketed) {
            SFX.cushion();
            stateNow.lastCushionAt = now;
          }
          stateNow.movingIds = movingIds;
          stateNow.wallIds = wallIds;
          stateNow.frameIndex = soundFrameIndex;
        }
      }

      const remoteAimState = opponentAim && opponentAim.userId !== currentUserId ? opponentAim : null;
      const remoteAimFresh = Boolean(remoteAimState && Date.now() - remoteAimState.updatedAt < REMOTE_AIM_STALE_MS);
      const remoteMode: AimPointerMode = remoteAimState?.mode ?? "idle";
      const remoteCanRender = Boolean(
        remoteAimFresh
        && remoteAimState
        && remoteAimState.visible
        && remoteMode !== "idle"
        && game.status === "waiting_shot"
      );
      const remoteCuePlacementActive = Boolean(
        remoteAimFresh
        && remoteAimState
        && remoteMode === "place"
        && game.status === "waiting_shot"
        && game.ballInHandUserId === remoteAimState.userId
        && remoteAimState.cueX !== null
        && remoteAimState.cueY !== null
      );
      const authoritativeRemoteCue = drawCueBall ?? cueBall ?? {
        id: "ball-0-remote-authoritative",
        number: 0,
        x: DEFAULT_CUE_X,
        y: DEFAULT_CUE_Y,
        pocketed: false,
      };
      const remoteCueSource = remoteCanRender || remoteCuePlacementActive
        ? {
            id: "ball-0-remote-overlay",
            number: 0,
            x: remoteCuePlacementActive || remoteMode === "place"
              ? (remoteAimState?.cueX ?? authoritativeRemoteCue.x)
              : authoritativeRemoteCue.x,
            y: remoteCuePlacementActive || remoteMode === "place"
              ? (remoteAimState?.cueY ?? authoritativeRemoteCue.y)
              : authoritativeRemoteCue.y,
            pocketed: false,
          }
        : null;
      const remoteVisual = remoteAimVisualRef.current;
      let remoteCueBall: GameBallSnapshot | null = null;
      let remoteAimAngle = 0;
      let remotePullRatio = 0.05;

      if ((remoteCanRender || remoteCuePlacementActive) && remoteAimState && remoteCueSource) {
        const targetX = remoteCuePlacementActive || remoteMode === "place"
          ? (remoteAimState.cueX ?? remoteCueSource.x)
          : remoteCueSource.x;
        const targetY = remoteCuePlacementActive || remoteMode === "place"
          ? (remoteAimState.cueY ?? remoteCueSource.y)
          : remoteCueSource.y;
        const targetAngle = remoteAimState.angle;
        const targetPull = remoteCanRender ? clamp(remoteAimState.power ?? 0, 0, 1) : 0;
        const hardSnap = !remoteVisual.initialized
          || remoteAimState.seq < remoteVisual.seq
          || Math.abs(targetX - remoteVisual.x) > 180
          || Math.abs(targetY - remoteVisual.y) > 180;

        if (hardSnap) {
          remoteVisual.x = targetX;
          remoteVisual.y = targetY;
          remoteVisual.angle = targetAngle;
          remoteVisual.pull = targetPull;
          remoteVisual.seq = remoteAimState.seq;
          remoteVisual.initialized = true;
        } else {
          const posLerp = remoteCuePlacementActive || remoteMode === "place" ? 0.72 : 0.42;
          const angleLerp = remoteCanRender ? (remoteMode === "aim" ? 0.36 : remoteMode === "power" ? 0.3 : 0.2) : 0.18;
          const pullLerp = remoteCanRender && remoteMode === "power" ? 0.5 : 0.26;
          remoteVisual.x = lerp(remoteVisual.x, targetX, posLerp);
          remoteVisual.y = lerp(remoteVisual.y, targetY, posLerp);
          remoteVisual.angle = lerpAngle(remoteVisual.angle, targetAngle, angleLerp);
          remoteVisual.pull = lerp(remoteVisual.pull, targetPull, pullLerp);
          remoteVisual.seq = Math.max(remoteVisual.seq, remoteAimState.seq);
          remoteVisual.initialized = true;
        }

        remoteCueBall = {
          id: remoteCueSource.id,
          number: 0,
          x: remoteVisual.x,
          y: remoteVisual.y,
          pocketed: false,
        };
        remoteAimAngle = remoteVisual.angle;
        remotePullRatio = remoteCanRender
          ? (remoteMode === "power"
              ? clamp(0.2 + remoteVisual.pull * 0.8, 0.2, 0.98)
              : remoteMode === "aim"
                ? 0.1
                : remoteMode === "place"
                  ? 0.08
                  : 0.05)
          : 0.08;
      } else if (!remoteAimFresh || (!remoteCanRender && !remoteCuePlacementActive) || game.status !== "waiting_shot") {
        remoteVisual.initialized = false;
      }

      if (remoteCueBall && remoteCuePlacementActive) {
        let replacedCue = false;
        drawBalls = drawBalls.map((ball) => {
          if (ball.number !== 0) return ball;
          replacedCue = true;
          return { ...ball, x: remoteCueBall.x, y: remoteCueBall.y, pocketed: false };
        });
        if (!replacedCue) drawBalls = [remoteCueBall, ...drawBalls];
        drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? remoteCueBall;
      }

      const remoteOverlayVisible = Boolean(
        remoteCanRender
        && remoteAimState
        && remoteCueBall
        && (remoteMode === "aim" || remoteMode === "power" || remoteMode === "place")
      );

      updateBallSpinCache(ballSpinRef.current, drawBalls, now);
      const pendingShotVisual = pendingShotVisualRef.current;
      if (!animating && pendingShotVisual && drawCueBall) {
        const elapsed = now - pendingShotVisual.startedAt;
        // Only expire pending visual when NOT simulating AND past max time
        const shouldExpirePending = elapsed >= PENDING_SHOT_VISUAL_MAX_MS && game.status !== "simulating";
        if (shouldExpirePending) {
          // Sync cue ball in visual ref to current server-smoothed position before clearing
          const serverCue = drawBalls.find(b => b.number === 0 && !b.pocketed);
          if (serverCue) {
            realtimeVisualBallsRef.current = realtimeVisualBallsRef.current.map(
              b => b.number === 0 ? { ...b, x: serverCue.x, y: serverCue.y } : b
            );
            const spin = ballSpinRef.current.get(serverCue.id);
            if (spin) { spin.lastX = serverCue.x; spin.lastY = serverCue.y; }
          }
          pendingShotVisualRef.current = null;
        } else if (game.status === "simulating" && elapsed > PENDING_SHOT_VISUAL_MAX_MS) {
          // During simulation after max time, stop moving the cue optimistically
          // but keep the ref alive to protect the cue ball from vanishing
        } else {
          const progress = computePendingShotProgress(pendingShotVisual, now);
          const heldElapsed = progress.heldElapsed;
          const travelMs = progress.travelMs;
          const moveProgress = progress.moveProgress;
          const optimisticCue = {
            ...drawCueBall,
            x: progress.expectedCueX,
            y: progress.expectedCueY,
            pocketed: false,
          };
          drawBalls = drawBalls.map((ball) => (ball.number === 0 ? optimisticCue : ball));
          drawCueBall = optimisticCue;
          // Sync visual ref so next frame lerp starts from optimistic pos, eliminating rollback
          realtimeVisualBallsRef.current = realtimeVisualBallsRef.current.map(
            b => b.number === 0 ? { ...b, x: optimisticCue.x, y: optimisticCue.y } : b
          );
          if (pendingShotVisual.impactType && !pendingShotVisual.firstImpactPlayed && pendingShotVisual.impactAtMs !== null && heldElapsed >= pendingShotVisual.impactAtMs) {
            pendingShotVisual.firstImpactPlayed = true;
          }
          if (moveProgress >= 1 && heldElapsed >= travelMs + PENDING_SHOT_POST_IMPACT_HOLD_MS && game.status !== "simulating") {
            pendingShotVisualRef.current = null;
          }
        }
      }

      const preview = drawCueBall && !animating && !pendingShotVisualRef.current && !(state.isBallInHand && state.pointerMode === "place") ? computeAimPreview(drawCueBall, drawBalls, drawAimAngleRef.current) : null;
      const illegalTarget = preview ? isAimTargetIllegal(preview, drawBalls, displayedGroupsRef.current.hostGroup, displayedGroupsRef.current.guestGroup, currentUserId, room.hostUserId) : false;
      const previewPowerRatio = state.pointerMode === "power"
        ? clamp((state.power - POWER_MIN) / (1 - POWER_MIN), 0, 1)
        : 0.56;
      const remotePreview = remoteOverlayVisible && remoteCueBall && remoteMode !== "place"
        ? computeAimPreview(remoteCueBall, drawBalls, remoteAimAngle)
        : null;

      // Quick cue settle after release for responsiveness
      const SNAP_MS = 14;
      const snap = snapAnimRef.current;
      let pullRatio = 0;
      if (snap) {
        const elapsed = now - snap.startedAt;
        if (elapsed >= SNAP_MS) {
          pullRatio = 0;
          snapAnimRef.current = null;
        } else {
          const t = elapsed / SNAP_MS;
          const eased = 1 - (1 - t) * (1 - t);
          pullRatio = clamp(0.16 + snap.power * 0.78, 0.16, 1) * (1 - eased);
        }
      } else if (state.pointerMode === "power") {
        pullRatio = clamp(0.15 + state.power * 0.76, 0.15, 0.95);
      } else if (state.pointerMode === "aim") {
        pullRatio = 0.06;
      }

      drawPoolTable(
        context,
        tableCacheRef.current,
        cueSprite,
        drawBalls,
        drawCueBall,
        drawAimAngleRef.current,
        Boolean(drawCueBall && (state.canInteract || snapAnimRef.current) && !pendingShotVisualRef.current && !(state.isBallInHand && state.pointerMode === "place")),
        pullRatio,
        preview,
        previewPowerRatio,
        illegalTarget,
        state.needEightCall,
        state.selectedPocket,
        state.isBallInHand,
        pocketAnimationsRef.current,
        now,
        ballSpinRef.current,
        remoteOverlayVisible && remoteCueBall ? { cueBall: remoteCueBall, aimAngle: remoteAimAngle, preview: remotePreview, pullRatio: remotePullRatio, mode: remoteMode } : null,
      );

      drawLoopRef.current = window.requestAnimationFrame(draw);
    };

    drawLoopRef.current = window.requestAnimationFrame(draw);
    return () => { if (drawLoopRef.current !== null) { window.cancelAnimationFrame(drawLoopRef.current); drawLoopRef.current = null; } };
  }, [animating, assetsVersion, cueSprite, currentUserId, game.ballInHandUserId, game.status, game.turnUserId, opponentAim, room.hostUserId]);

  // ─── Pocketed ball mini-icons (for HUD) ─────────────────────────────────
  // Pre-render mini ball icons as data URLs for HUD pips
  const ballIconCache = useMemo(() => {
    const cache = new Map<number, string>();
    const size = 30; // render at 2x for sharpness
    for (let n = 1; n <= 15; n++) {
      const c = document.createElement("canvas");
      c.width = size; c.height = size;
      const cx = c.getContext("2d");
      if (!cx) continue;
      const r = size / 2 - 1;
      const color = ballColor(n);
      const isStripe = n >= 9;
      cx.translate(size / 2, size / 2);
      // Base
      const bg = cx.createRadialGradient(-r * 0.25, -r * 0.25, r * 0.05, 0, 0, r * 1.2);
      if (n === 8) {
        bg.addColorStop(0, "#4a5260"); bg.addColorStop(0.3, "#1e2330"); bg.addColorStop(1, "#050709");
      } else {
        bg.addColorStop(0, "#fff8d8"); bg.addColorStop(0.18, color); bg.addColorStop(1, shadeColor(color, -50));
      }
      cx.beginPath(); cx.arc(0, 0, r, 0, Math.PI * 2); cx.fillStyle = bg; cx.fill();
      // Stripe
      if (isStripe) {
        cx.beginPath(); cx.arc(0, 0, r - 0.5, 0, Math.PI * 2); cx.fillStyle = "#f8faff"; cx.fill();
        cx.save(); cx.beginPath(); cx.arc(0, 0, r, 0, Math.PI * 2); cx.clip();
        cx.fillStyle = color; cx.fillRect(-r, -r * 0.55, r * 2, r * 1.1); cx.restore();
      }
      // Number disk
      const dr = r * 0.42;
      cx.beginPath(); cx.arc(0, 0, dr, 0, Math.PI * 2); cx.fillStyle = "#fff"; cx.fill();
      cx.font = `700 ${n >= 10 ? 7 : 8}px Inter, system-ui, sans-serif`;
      cx.textAlign = "center"; cx.textBaseline = "middle";
      cx.fillStyle = "#1a1e2a"; cx.fillText(String(n), 0, 0.5);
      // Specular
      const sg = cx.createRadialGradient(-r * 0.3, -r * 0.35, 0, -r * 0.25, -r * 0.28, r * 0.5);
      sg.addColorStop(0, "rgba(255,255,255,0.6)"); sg.addColorStop(0.4, "rgba(255,255,255,0.15)"); sg.addColorStop(1, "rgba(255,255,255,0)");
      cx.beginPath(); cx.arc(0, 0, r, 0, Math.PI * 2); cx.fillStyle = sg; cx.fill();
      cache.set(n, c.toDataURL());
    }
    return cache;
  }, []);

  function BallPip({ number }: { number: number }) {
    const src = ballIconCache.get(number);
    if (!src) return <span className="pool-stage__pip" />;
    return (
      <span className="pool-stage__pip pool-stage__pip--ball">
        <img src={src} alt={String(number)} style={{ width: "100%", height: "100%", borderRadius: "50%" }} />
      </span>
    );
  }

  const railEntries = railBalls.map((entry) => ({
    entry,
    src: ballIconCache.get(entry.number),
  }));

  // ─── JSX ────────────────────────────────────────────────────────────────

  return (
    <section className="pool-stage" aria-label="Mesa de sinuca">
      <div className="pool-stage__hud">
        <button
          className="pool-stage__menu"
          type="button"
          disabled={exitBusy}
          onClick={onExit}
          aria-label={isHost ? "Fechar sala" : "Sair"}
          title={isHost ? "Fechar sala" : "Sair"}
        >
          {exitBusy ? "…" : "≡"}
        </button>

        <div className={`pool-stage__player-side pool-stage__player-side--left ${game.turnUserId === leftPlayer?.userId ? "pool-stage__player-side--active" : ""}`}>
          <strong>{cleanName(leftPlayer?.displayName ?? "Jogador")}</strong>
          <div className="pool-stage__pips">
            {Array.from({ length: 7 }).map((_, index) => {
              const number = leftPocketed[index] ?? null;
              return number !== null
                ? <BallPip key={`left-${index}`} number={number} />
                : <span key={`left-${index}`} className="pool-stage__pip" />;
            })}
          </div>
        </div>

        <div className="pool-stage__hud-center">
          <div className={`pool-stage__avatar ${game.turnUserId === leftPlayer?.userId ? "pool-stage__avatar--active" : ""}`}>
            {leftPlayer?.avatarUrl ? <img src={leftPlayer.avatarUrl} alt={cleanName(leftPlayer.displayName)} /> : <span>{playerInitials(leftPlayer)}</span>}
          </div>
          <div className="pool-stage__status-badge">
            <span className="pool-stage__stake">{cueLabel}</span>
            <strong>{statusText}</strong>
            <small>{phaseText}</small>
          </div>
          <div className={`pool-stage__avatar ${game.turnUserId === rightPlayer?.userId ? "pool-stage__avatar--active" : ""}`}>
            {rightPlayer?.avatarUrl ? <img src={rightPlayer.avatarUrl} alt={cleanName(rightPlayer.displayName)} /> : <span>{playerInitials(rightPlayer)}</span>}
          </div>
        </div>

        <div className={`pool-stage__player-side pool-stage__player-side--right ${game.turnUserId === rightPlayer?.userId ? "pool-stage__player-side--active" : ""}`}>
          <div className="pool-stage__pips pool-stage__pips--right">
            {Array.from({ length: 7 }).map((_, index) => {
              const number = rightPocketed[index] ?? null;
              return number !== null
                ? <BallPip key={`right-${index}`} number={number} />
                : <span key={`right-${index}`} className="pool-stage__pip" />;
            })}
          </div>
          <strong>{cleanName(rightPlayer?.displayName ?? "Adversário")}</strong>
        </div>

        {(() => {
          const cuePocketed = game.balls.find(b => b.number === 0)?.pocketed ?? false;
          const showCueIndicator = cuePocketed || game.ballInHandUserId != null;
          return showCueIndicator ? (
            <div className="pool-stage__cue-indicator" aria-label="Bola branca fora da mesa" />
          ) : null;
        })()}
      </div>

      <div className="pool-stage__table-layout">
        <div
          ref={powerRailRef}
          className={`pool-stage__power ${powerBarInteractive ? "pool-stage__power--active" : ""} ${pointerMode === "power" ? "pool-stage__power--dragging" : ""} ${turnControlVisible ? "" : "pool-stage__power--hidden"} ${isCueBallPlacementActive && turnControlVisible ? "pool-stage__power--standby" : ""}`}
          aria-hidden={!turnControlVisible}
          onPointerDown={handlePowerDown}
          onPointerMove={handlePowerMove}
          onPointerUp={handlePowerUp}
          onPointerCancel={handlePowerCancel}
          onTouchStart={handlePowerTouchStart}
          onTouchMove={handlePowerTouchMove}
          onTouchEnd={handlePowerTouchEnd}
          onTouchCancel={handlePowerTouchCancel}
          onLostPointerCapture={handlePowerLostCapture}
        >
          <div ref={powerTrackRef} className="pool-stage__power-track">
            <div
              className="pool-stage__power-gradient"
              style={{ clipPath: `inset(0 0 calc(${((1 - displayedPowerCueTop) * 100).toFixed(1)}% + 10px) 0)` }}
            />
            <div className="pool-stage__power-guides" />
            <div className="pool-stage__power-cue" style={{ top: `calc(${(displayedPowerCueTop * 100).toFixed(1)}% - 2px)` }}>
              <span className="pool-stage__power-cue-tip" />
              <span className="pool-stage__power-cue-ferrule" />
              <span className="pool-stage__power-cue-shaft" />
              <span className="pool-stage__power-cue-butt" />
            </div>
          </div>
        </div>

        <div className="pool-stage__table-shell">
          <div
            ref={tableWrapRef}
            className={`pool-stage__table-wrap ${canInteract ? "pool-stage__table-wrap--interactive" : ""}`}
            onPointerDown={handleTablePointerDown}
            onPointerMove={handleTablePointerMove}
            onPointerUp={handleTablePointerUp}
            onPointerCancel={handleTablePointerUp}
            onPointerLeave={(event) => { if (pointerMode === "idle") return; handleTablePointerUp(event); }}
          >
            <canvas ref={canvasRef} className="pool-stage__canvas" aria-hidden="true" />
            {groupBanner && (
              <div className="pool-stage__group-banner">{groupBanner}</div>
            )}
            {needEightCall && isMyTurn ? POCKETS.map((pocket) => (
              <button
                key={pocket.id}
                type="button"
                className={`pool-stage__pocket-call ${selectedPocket === pocket.id ? "pool-stage__pocket-call--active" : ""}`}
                style={{ left: `${(pocket.x / TABLE_WIDTH) * 100}%`, top: `${(pocket.y / TABLE_HEIGHT) * 100}%` }}
                onClick={() => setSelectedPocket(pocket.id)}
                aria-label={`Escolher caçapa ${pocket.id}`}
              >
                {pocket.id}
              </button>
            )) : null}
          </div>
        </div>

        <aside className="pool-stage__return-rail" aria-hidden="true">
          <div className="pool-stage__rail-arm-h" />
          <div className="pool-stage__rail-arm-v">
            <div className="pool-stage__rail-channel">
              {railEntries.map(({ entry, src }) => (
                <span
                  key={entry.id}
                  className={`pool-stage__rail-ball pool-stage__rail-ball--${entry.state}`}
                  style={{ '--rail-slot': entry.slot } as CSSProperties}
                >
                  {src ? <img src={src} alt="" /> : null}
                </span>
              ))}
            </div>
          </div>
        </aside>
      </div>

      {game.status === "finished" && (() => {
        const isWinner = game.winnerUserId === currentUserId;
        const rematchReady = room.rematchReadyUserIds ?? [];
        const readyCount = rematchReady.length;
        const myReady = rematchReady.includes(currentUserId);
        return (
          <div className="pool-stage__endgame-overlay">
            <div className="pool-stage__endgame-card">
              <img
                className="pool-stage__endgame-emoji"
                src={isWinner
                  ? "https://cdn.discordapp.com/emojis/1485043651292827788.webp?size=96"
                  : "https://cdn.discordapp.com/emojis/1485043671077228786.webp?size=96"
                }
                alt={isWinner ? "Vitória" : "Derrota"}
                width="56"
                height="56"
                draggable={false}
              />
              <h2 className="pool-stage__endgame-title">
                {isWinner ? "Você venceu!" : "Você perdeu!"}
              </h2>
              {game.tableType !== "casual" && game.stakeChips ? (
                <div className={`pool-stage__endgame-chips ${isWinner ? "pool-stage__endgame-chips--win" : "pool-stage__endgame-chips--lose"}`}>
                  {isWinner ? "+" : "-"}{game.stakeChips} fichas
                </div>
              ) : (
                <div className="pool-stage__endgame-chips pool-stage__endgame-chips--casual">
                  Amistoso
                </div>
              )}
              <div className="pool-stage__endgame-actions">
                <button
                  type="button"
                  className="pool-stage__endgame-btn pool-stage__endgame-btn--lobby"
                  disabled={exitBusy}
                  onClick={onExit}
                >
                  Lobby
                </button>
                <button
                  type="button"
                  className={`pool-stage__endgame-btn pool-stage__endgame-btn--again ${myReady ? "pool-stage__endgame-btn--ready" : ""}`}
                  disabled={!onRematchReady || exitBusy || myReady}
                  onClick={() => onRematchReady?.()}
                >
                  {myReady ? `Aguardando... (${readyCount}/2)` : `Jogar novamente (${readyCount}/2)`}
                </button>
              </div>
            </div>
          </div>
        );
      })()}
    </section>
  );
}
