import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type TouchEvent as ReactTouchEvent } from "react";
import type { AimPointerMode, AimStateSnapshot, BallGroup, GameBallSnapshot, GameShotFrame, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
import { drawAimLine, drawCue, drawGhostBall, drawRemoteAimOverlay, type AimPreview } from "./gameStageAimRender";
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
const BALL_VISUAL_RADIUS = 17;
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
const REALTIME_RENDER_DELAY_MS = 72;
const CUE_SHOT_START_LOCK_MS = 160;
const CUE_SHOT_START_RAMP_MS = 120;
const REALTIME_MAX_EXTRAPOLATION_MS = 28;
const PENDING_SHOT_VISUAL_MAX_MS = 1150;
const PENDING_SHOT_POST_IMPACT_HOLD_MS = 240;
const SERVER_MIN_SHOT_SPEED = 0.014;
const SERVER_MAX_SHOT_SPEED = 12.6;
const SERVER_REALTIME_SPEED_TO_PX_PER_MS = 5.2 / (1000 / 60);
const SNAPSHOT_RENDER_DEBUG_ENABLED = false;
const SNAPSHOT_RENDER_DEBUG_LOG_EVERY_MS = 450;
const POCKET_CAPTURE_DISTANCE = BALL_RADIUS * 1.6;
const RAIL_TRAVEL_DURATION_MS = 1100;
const RAIL_SETTLE_DELAY_MS = 110;
const CUE_RETURN_HOLD_MS = 420;
const BALL_SPRITE_SIZE = 72;
const AIM_RENDER_METRICS = { ballRadius: BALL_RADIUS, ballVisualRadius: BALL_VISUAL_RADIUS } as const;

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

type Quaternion = {
  x: number;
  y: number;
  z: number;
  w: number;
};

type Vec3 = {
  x: number;
  y: number;
  z: number;
};

type BallSpinState = {
  orientation: Quaternion;
  lastX: number;
  lastY: number;
  lastSeenAt: number;
  visualSpeed: number;
  rollVelocity: number;
  sideVelocity: number;
  bankVelocity: number;
  lastHeading: number | null;
  restFrames: number;
  rollPhase: number;
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

function smoothstep(edge0: number, edge1: number, value: number) {
  if (edge0 === edge1) return value < edge0 ? 0 : 1;
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

function shapeShotPowerForVisual(power: number) {
  const shotPower = clamp(Number.isFinite(power) ? power : 0.52, 0.00035, 1);
  return shotPower <= 0.36
    ? shotPower * 0.92
    : shotPower <= 0.82
      ? 0.3312 + Math.pow((shotPower - 0.36) / 0.46, 1.42) * 0.3788
      : 0.71 + Math.pow((shotPower - 0.82) / 0.18, 1.88) * 0.29;
}

function estimateCueVisualSpeedPxPerMs(power: number) {
  const shapedPower = shapeShotPowerForVisual(power);
  const shotSpeed = SERVER_MIN_SHOT_SPEED + shapedPower * SERVER_MAX_SHOT_SPEED;
  return clamp(shotSpeed * SERVER_REALTIME_SPEED_TO_PX_PER_MS, 0.025, 3.55);
}

function maxCueVisualLeadPx(power: number) {
  return lerp(0.18, 2.1, Math.pow(clamp(power, 0, 1), 1.12));
}

function maxCueAdvanceStepPx(power: number, estimatedSpeedPxPerMs: number, deltaMs: number) {
  const shapedPower = Math.pow(clamp(power, 0, 1), 1.18);
  const conservativeSpeed = estimatedSpeedPxPerMs * lerp(0.38, 0.68, shapedPower);
  return Math.max(0.18, conservativeSpeed * Math.max(1, deltaMs));
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

function angleDelta(target: number, current: number) {
  let delta = target - current;
  while (delta > Math.PI) delta -= Math.PI * 2;
  while (delta < -Math.PI) delta += Math.PI * 2;
  return delta;
}

function normalizeVec3(vec: Vec3): Vec3 {
  const len = Math.hypot(vec.x, vec.y, vec.z) || 1;
  return { x: vec.x / len, y: vec.y / len, z: vec.z / len };
}

function multiplyQuaternion(a: Quaternion, b: Quaternion): Quaternion {
  return {
    w: a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
    x: a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
    y: a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
    z: a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
  };
}

function normalizeQuaternion(q: Quaternion): Quaternion {
  const len = Math.hypot(q.x, q.y, q.z, q.w) || 1;
  return { x: q.x / len, y: q.y / len, z: q.z / len, w: q.w / len };
}

function applyOrientationStep(orientation: Quaternion, axis: Vec3, angle: number): Quaternion {
  if (!Number.isFinite(angle) || Math.abs(angle) < 0.000001) return orientation;
  const normalizedAxis = normalizeVec3(axis);
  const half = angle * 0.5;
  const s = Math.sin(half);
  const step = {
    x: normalizedAxis.x * s,
    y: normalizedAxis.y * s,
    z: normalizedAxis.z * s,
    w: Math.cos(half),
  };
  return normalizeQuaternion(multiplyQuaternion(step, orientation));
}

function rotateVectorByQuaternion(vec: Vec3, q: Quaternion): Vec3 {
  const qvec = { x: q.x, y: q.y, z: q.z };
  const uv = {
    x: qvec.y * vec.z - qvec.z * vec.y,
    y: qvec.z * vec.x - qvec.x * vec.z,
    z: qvec.x * vec.y - qvec.y * vec.x,
  };
  const uuv = {
    x: qvec.y * uv.z - qvec.z * uv.y,
    y: qvec.z * uv.x - qvec.x * uv.z,
    z: qvec.x * uv.y - qvec.y * uv.x,
  };
  return {
    x: vec.x + (uv.x * q.w + uuv.x) * 2,
    y: vec.y + (uv.y * q.w + uuv.y) * 2,
    z: vec.z + (uv.z * q.w + uuv.z) * 2,
  };
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
  skipCueBall = false,
) {
  if (!fromBalls.length) return targetBalls.map((ball) => ({ ...ball }));
  const fromMap = new Map(fromBalls.map((ball) => [ball.id, ball]));
  return targetBalls.map((ball) => {
    const prev = fromMap.get(ball.id);
    if (!prev) return { ...ball };
    if (prev.pocketed !== ball.pocketed) return { ...ball };
    // When pending shot is active, cue ball is controlled elsewhere — don't touch it
    if (skipCueBall && ball.number === 0) return prev;

    const dx = ball.x - prev.x;
    const dy = ball.y - prev.y;
    const dist = Math.hypot(dx, dy);
    const isCueBall = ball.number === 0;

    // Only hard-snap on truly huge jumps (teleport/state reset)
    const hardSnapDistance = isCueBall ? 900 : REALTIME_VISUAL_SNAP_DISTANCE * 5;
    if (dist >= hardSnapDistance) return { ...ball };

    // Always lerp — never snap prematurely. Let convergence happen naturally.
    const responseFloor = isCueBall ? 0.28 : 0.22;
    const responseCeil = isCueBall ? 0.90 : 0.85;
    const distanceBoost = clamp(dist / (isCueBall ? 16 : 12), 0, 1) * 0.32;
    const appliedAlpha = clamp(Math.max(alpha, responseFloor) + distanceBoost, responseFloor, responseCeil);
    return {
      ...ball,
      x: lerp(prev.x, ball.x, appliedAlpha),
      y: lerp(prev.y, ball.y, appliedAlpha),
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
  power: number;
  angle: number;
  cueX: number;
  cueY: number;
  travelLimit: number;
  estimatedSpeedPxPerMs: number;
  impactAtMs: number | null;
}, now: number) {
  const elapsed = now - pending.startedAt;
  const heldElapsed = Math.min(elapsed, PENDING_SHOT_VISUAL_MAX_MS);
  const travelMs = Math.max(22, pending.impactAtMs ?? Math.max(40, pending.travelLimit / Math.max(0.55, pending.estimatedSpeedPxPerMs)));
  const moveProgress = clamp(heldElapsed / travelMs, 0, 1);
  const conservativeDistance = heldElapsed * pending.estimatedSpeedPxPerMs;
  const limitedDistance = Math.min(pending.travelLimit, conservativeDistance);
  const expectedCueX = pending.cueX + Math.cos(pending.angle) * limitedDistance;
  const expectedCueY = pending.cueY + Math.sin(pending.angle) * limitedDistance;
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
        orientation: { x: 0, y: 0, z: 0, w: 1 },
        lastX: ball.x,
        lastY: ball.y,
        lastSeenAt: now,
        visualSpeed: 0,
        rollVelocity: 0,
        sideVelocity: 0,
        bankVelocity: 0,
        lastHeading: null,
        restFrames: 0,
        rollPhase: 0,
      });
      continue;
    }

    const elapsedMs = Math.max(1, now - current.lastSeenAt);
    const elapsedFrames = Math.max(1, elapsedMs / (1000 / 60));
    const dx = ball.x - current.lastX;
    const dy = ball.y - current.lastY;
    const rawDistance = Math.hypot(dx, dy);
    const visualSpeed = rawDistance / elapsedFrames;

    const TELEPORT_THRESHOLD = 18.0 * elapsedFrames;
    if (rawDistance > TELEPORT_THRESHOLD) {
      current.lastX = ball.x;
      current.lastY = ball.y;
      current.lastSeenAt = now;
      current.rollVelocity *= 0.55;
      current.sideVelocity *= 0.45;
      current.bankVelocity *= 0.4;
      current.visualSpeed *= 0.5;
      current.restFrames = 0;
      continue;
    }

    const velocityX = Number.isFinite(ball.velocityX) ? (ball.velocityX as number) : dx / elapsedFrames;
    const velocityY = Number.isFinite(ball.velocityY) ? (ball.velocityY as number) : dy / elapsedFrames;
    const velocitySpeed = Math.hypot(velocityX, velocityY);
    const rollSource = Number.isFinite(ball.spinRoll) ? (ball.spinRoll as number) : velocitySpeed;
    const sideSource = Number.isFinite(ball.spinSide) ? (ball.spinSide as number) : 0;
    const movementHeading = rawDistance > 0.055
      ? Math.atan2(dy, dx)
      : velocitySpeed > 0.11
        ? Math.atan2(velocityY, velocityX)
        : current.lastHeading;
    const moving = rawDistance > 0.022 || velocitySpeed > 0.03 || Math.abs(sideSource) > 0.025 || Math.abs(rollSource) > 0.03;
    const nearRest = rawDistance < 0.045 && velocitySpeed < 0.08 && Math.abs(sideSource) < 0.04 && Math.abs(rollSource) < 0.08;

    if (moving && movementHeading !== null && !nearRest) {
      const rollFromTravel = rawDistance / (BALL_VISUAL_RADIUS * 1.02 * elapsedFrames);
      const rollFromState = rollSource / (BALL_VISUAL_RADIUS * 1.06);
      const rollBlend = velocitySpeed > 0.02 ? 0.6 : 0.35;
      const targetRollVelocity = clamp(lerp(rollFromTravel, rollFromState, rollBlend), -1.18, 1.18);
      const targetSideVelocity = clamp(sideSource / (BALL_VISUAL_RADIUS * 4.3), -0.22, 0.22);
      const headingTurn = current.lastHeading === null ? 0 : angleDelta(movementHeading, current.lastHeading);
      const targetBankVelocity = clamp(headingTurn * 0.3 + targetSideVelocity * 0.46, -0.16, 0.16);
      const speedBlend = rawDistance > 1.35 ? 0.82 : rawDistance > 0.42 ? 0.68 : 0.42;
      current.rollVelocity = lerp(current.rollVelocity, targetRollVelocity, speedBlend);
      current.sideVelocity = lerp(current.sideVelocity, targetSideVelocity, 0.34 + speedBlend * 0.28);
      current.bankVelocity = lerp(current.bankVelocity, targetBankVelocity, 0.26 + speedBlend * 0.24);
      current.lastHeading = movementHeading;
      current.visualSpeed = lerp(current.visualSpeed, Math.max(visualSpeed, velocitySpeed), 0.82);
      current.restFrames = 0;
    } else {
      current.restFrames = Math.min(16, current.restFrames + 1);
      const decay = nearRest ? 0.74 : clamp(0.78 + current.visualSpeed * 0.24, 0.78, 0.97);
      current.rollVelocity *= decay;
      current.sideVelocity *= nearRest ? 0.78 : 0.9;
      current.bankVelocity *= nearRest ? 0.72 : 0.88;
      current.visualSpeed *= decay;
      if (Math.abs(current.rollVelocity) < 0.00045) current.rollVelocity = 0;
      if (Math.abs(current.sideVelocity) < 0.0003) current.sideVelocity = 0;
      if (Math.abs(current.bankVelocity) < 0.0003) current.bankVelocity = 0;
    }

    const effectiveHeading = current.lastHeading ?? movementHeading ?? 0;
    const rollAxis = {
      x: -Math.sin(effectiveHeading),
      y: Math.cos(effectiveHeading),
      z: 0,
    };
    const bankAxis = {
      x: Math.cos(effectiveHeading),
      y: Math.sin(effectiveHeading),
      z: 0,
    };

    let rollStep = current.rollVelocity * elapsedFrames;
    let sideStep = current.sideVelocity * elapsedFrames;
    let bankStep = current.bankVelocity * elapsedFrames;

    if (current.restFrames >= 2) {
      const settling = clamp((current.restFrames - 1) / 8, 0, 1);
      const freeze = 1 - settling;
      rollStep *= freeze;
      sideStep *= freeze * 0.72;
      bankStep *= freeze * 0.58;
    }

    let orientation = current.orientation;
    if (Math.abs(rollStep) > 0.00001) orientation = applyOrientationStep(orientation, rollAxis, rollStep);
    if (Math.abs(sideStep) > 0.00001) orientation = applyOrientationStep(orientation, { x: 0, y: 0, z: 1 }, sideStep);
    if (Math.abs(bankStep) > 0.00001) orientation = applyOrientationStep(orientation, bankAxis, bankStep);
    current.orientation = orientation;
    current.rollPhase += rollStep;

    current.lastX = ball.x;
    current.lastY = ball.y;
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

// ─── Single-atlas sprite sheet (1 canvas per ball, 24 frames) ─────────────
// Runtime cost: 1 drawImage with source rect per ball ≈ 0.01ms

const SPRITE_FRAMES = 24;
const SPRITE_PX = 44;
const ATLAS_WIDTH = SPRITE_PX * SPRITE_FRAMES;

const ballAtlasCache = new Map<number, HTMLCanvasElement>();
let atlasesReady = false;

function hexToRgbObj(hex: string) {
  const n = parseInt(hex.replace('#', ''), 16);
  return { r: (n >> 16) & 0xff, g: (n >> 8) & 0xff, b: n & 0xff };
}

function prerenderNumberTexture(ballNumber: number, size: number): Uint8Array {
  const c = document.createElement('canvas');
  c.width = size; c.height = size;
  const ctx = c.getContext('2d');
  if (!ctx) return new Uint8Array(size * size);
  const fontSize = ballNumber >= 10 ? Math.round(size * 0.40) : Math.round(size * 0.48);
  ctx.font = `800 ${fontSize}px Inter, system-ui, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  // Outline for legibility (especially ball 8)
  ctx.strokeStyle = 'rgba(255,255,255,0.5)';
  ctx.lineWidth = Math.max(1, size * 0.04);
  ctx.strokeText(String(ballNumber), size / 2, size / 2 + 1);
  ctx.fillStyle = '#000000';
  ctx.fillText(String(ballNumber), size / 2, size / 2 + 1);
  const img = ctx.getImageData(0, 0, size, size);
  const alpha = new Uint8Array(size * size);
  for (let i = 0; i < size * size; i++) alpha[i] = img.data[i * 4 + 3];
  return alpha;
}

function buildBallAtlas(ballNumber: number): HTMLCanvasElement {
  const existing = ballAtlasCache.get(ballNumber);
  if (existing) return existing;

  const S = SPRITE_PX;
  const R = S * 0.5 - 2;
  const C = S * 0.5;
  const color = ballColor(ballNumber);
  const rgb = hexToRgbObj(color);
  const isStripe = ballNumber >= 9;
  const isEight = ballNumber === 8;

  // Pre-render number into texture for sampling
  const numTexSize = Math.round(R * 1.6);
  const numAlpha = ballNumber > 0 ? prerenderNumberTexture(ballNumber, numTexSize) : null;
  // Disk cap radius: sqrt(1 - 0.82²) ≈ 0.572
  const diskCapR = 0.572;

  // Light direction (normalized)
  const lx = -0.36, ly = -0.50, lz = 0.78;
  const lLen = Math.hypot(lx, ly, lz);
  const ldx = lx / lLen, ldy = ly / lLen, ldz = lz / lLen;

  // Pre-compute sin/cos for all frames
  const cosTable = new Float32Array(SPRITE_FRAMES);
  const sinTable = new Float32Array(SPRITE_FRAMES);
  for (let f = 0; f < SPRITE_FRAMES; f++) {
    const a = (f / SPRITE_FRAMES) * Math.PI * 2;
    cosTable[f] = Math.cos(a);
    sinTable[f] = Math.sin(a);
  }

  const atlas = document.createElement('canvas');
  atlas.width = ATLAS_WIDTH;
  atlas.height = S;
  const ctx = atlas.getContext('2d');
  if (!ctx) { ballAtlasCache.set(ballNumber, atlas); return atlas; }

  const imageData = ctx.createImageData(ATLAS_WIDTH, S);
  const data = imageData.data;

  for (let f = 0; f < SPRITE_FRAMES; f++) {
    const cosA = cosTable[f];
    const sinA = sinTable[f];
    const offsetX = f * S;

    for (let y = 0; y < S; y++) {
      for (let x = 0; x < S; x++) {
        const px = (x + 0.5 - C) / R;
        const py = (y + 0.5 - C) / R;
        const rr = px * px + py * py;
        const idx = (y * ATLAS_WIDTH + offsetX + x) * 4;

        if (rr > 1) { data[idx + 3] = 0; continue; }

        const pz = Math.sqrt(1 - rr);

        // Rotate around X axis (rolling forward)
        const rx = px;
        const ry = py * cosA - pz * sinA;
        const rz = py * sinA + pz * cosA;

        // Surface color
        let cr: number, cg: number, cb: number;
        if (isEight) {
          cr = 18; cg = 22; cb = 30;
        } else if (isStripe) {
          // Stripe with softer feather (0.10) for smooth sphere wrap
          const absRy = Math.abs(ry);
          const edge = 0.46, fe = 0.10;
          const mask = absRy < edge - fe ? 1 : absRy > edge + fe ? 0 : 1 - (absRy - edge + fe) / (fe * 2);
          cr = 242 + ((rgb.r - 242) * mask) | 0;
          cg = 245 + ((rgb.g - 245) * mask) | 0;
          cb = 250 + ((rgb.b - 250) * mask) | 0;
        } else {
          cr = rgb.r; cg = rgb.g; cb = rgb.b;
        }

        // Number disk (white circle at front pole only) + number text
        let skipSpecular = false;
        if (ballNumber > 0) {
          const frontRz = rz;
          if (frontRz > 0.76) {
            const diskMask = frontRz < 0.82 ? (frontRz - 0.76) / 0.06 : 1;
            cr = (cr + (252 - cr) * diskMask) | 0;
            cg = (cg + (252 - cg) * diskMask) | 0;
            cb = (cb + (254 - cb) * diskMask) | 0;
            if (frontRz > 0.76) skipSpecular = true;

            // Sample number texture using sphere-local coords on the visible front pole only.
            if (numAlpha && diskMask > 0.2) {
              const texX = (rx / diskCapR * 0.5 + 0.5) * numTexSize;
              const texY = (ry / diskCapR * 0.5 + 0.5) * numTexSize;
              const txi = texX | 0;
              const tyi = texY | 0;
              if (txi >= 0 && txi < numTexSize && tyi >= 0 && tyi < numTexSize) {
                const numMask = numAlpha[tyi * numTexSize + txi] / 255;
                if (numMask > 0.05) {
                  const textR = isEight ? 228 : 20;
                  const textG = isEight ? 234 : 24;
                  const textB = isEight ? 242 : 34;
                  const blend = numMask * diskMask;
                  cr = (cr + (textR - cr) * blend) | 0;
                  cg = (cg + (textG - cg) * blend) | 0;
                  cb = (cb + (textB - cb) * blend) | 0;
                }
              }
            }
          }
        }

        // Lambert lighting
        const lambert = px * ldx + py * ldy + pz * ldz;
        const lClamped = lambert > 0 ? (lambert < 1 ? lambert : 1) : 0;
        const rim = 1 - pz;
        const shade = 0.38 + lClamped * 0.70 - rim * rim * 0.12;
        const s = shade < 0.22 ? 0.22 : shade > 1.12 ? 1.12 : shade;
        cr = (cr * s) | 0; if (cr > 255) cr = 255;
        cg = (cg * s) | 0; if (cg > 255) cg = 255;
        cb = (cb * s) | 0; if (cb > 255) cb = 255;

        // Ambient occlusion at bottom (simulates felt contact)
        if (py > 0.5) {
          const aoMask = (py - 0.5) * 0.18;
          cr = (cr * (1 - aoMask)) | 0;
          cg = (cg * (1 - aoMask)) | 0;
          cb = (cb * (1 - aoMask)) | 0;
        }

        // Primary specular (main light reflection)
        const specBase = -0.34 * ldx - 0.38 * ldy + pz * ldz;
        if (!skipSpecular && specBase > 0.62) {
          const specI = (specBase - 0.62) / 0.38;
          const spec = specI * specI * specI * 0.52 * 255;
          cr = Math.min(255, cr + spec | 0);
          cg = Math.min(255, cg + spec | 0);
          cb = Math.min(255, cb + spec | 0);
        }

        // Secondary specular (window reflection — smaller, offset)
        const spec2X = 0.22, spec2Y = -0.28;
        const spec2Dot = px * spec2X + py * spec2Y + pz * 0.93;
        if (!skipSpecular && spec2Dot > 0.88) {
          const s2 = (spec2Dot - 0.88) / 0.12;
          const spec2 = s2 * s2 * 0.35 * 255;
          cr = Math.min(255, cr + spec2 | 0);
          cg = Math.min(255, cg + spec2 | 0);
          cb = Math.min(255, cb + spec2 | 0);
        }

        // Edge AA
        const edgeAlpha = rr > 0.91 ? (1 - rr) / 0.09 : 1;
        data[idx] = cr;
        data[idx + 1] = cg;
        data[idx + 2] = cb;
        data[idx + 3] = (edgeAlpha * 255 + 0.5) | 0;
      }
    }
  }

  ctx.putImageData(imageData, 0, 0);
  ballAtlasCache.set(ballNumber, atlas);
  return atlas;
}

function ensureAllAtlases() {
  if (atlasesReady) return;
  for (let n = 1; n <= 15; n++) buildBallAtlas(n);
  atlasesReady = true;
}

// Pre-rendered cue ball sprite (eliminates per-frame gradient creation)
let cueBallSprite: HTMLCanvasElement | null = null;

function getCueBallSprite(): HTMLCanvasElement {
  if (cueBallSprite) return cueBallSprite;
  const size = SPRITE_PX;
  const r = size * 0.5 - 2;
  const c = size * 0.5;
  const canvas = document.createElement('canvas');
  canvas.width = size; canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) return canvas;
  const g = ctx.createRadialGradient(c - r * 0.28, c - r * 0.28, r * 0.05, c, c, r * 1.2);
  g.addColorStop(0, "#ffffff");
  g.addColorStop(0.35, "#f0f4fa");
  g.addColorStop(0.70, "#d8e4f0");
  g.addColorStop(1, "#a8bed8");
  ctx.beginPath();
  ctx.arc(c, c, r, 0, Math.PI * 2);
  ctx.fillStyle = g;
  ctx.fill();
  // Specular highlight
  const sg = ctx.createRadialGradient(c - r * 0.32, c - r * 0.36, 0, c - r * 0.26, c - r * 0.28, r * 0.48);
  sg.addColorStop(0, "rgba(255,255,255,0.82)");
  sg.addColorStop(0.4, "rgba(255,255,255,0.25)");
  sg.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath();
  ctx.arc(c, c, r, 0, Math.PI * 2);
  ctx.fillStyle = sg;
  ctx.fill();
  // Second specular (window)
  const s2 = ctx.createRadialGradient(c + r * 0.18, c - r * 0.24, 0, c + r * 0.20, c - r * 0.22, r * 0.22);
  s2.addColorStop(0, "rgba(255,255,255,0.38)");
  s2.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath();
  ctx.arc(c, c, r, 0, Math.PI * 2);
  ctx.fillStyle = s2;
  ctx.fill();
  // Outline
  ctx.beginPath();
  ctx.arc(c, c, r - 0.4, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(0,0,0,0.14)";
  ctx.lineWidth = 0.8;
  ctx.stroke();
  cueBallSprite = canvas;
  return canvas;
}

function drawBall(
  ctx: CanvasRenderingContext2D,
  ball: GameBallSnapshot,
  scale = 1,
  spin: BallSpinState | undefined = undefined,
) {
  const r = BALL_VISUAL_RADIUS * scale;
  const isFullSize = scale > 0.9;

  ctx.save();
  ctx.translate(ball.x, ball.y);

  // Contact shadow + felt reflection only at full size (skip during pocket anim)
  if (isFullSize) {
    ctx.save();
    ctx.globalAlpha = 0.26;
    ctx.beginPath();
    ctx.ellipse(0, r * 0.82, r * 0.70, r * 0.17, 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fill();
    ctx.restore();

    if (ball.number > 0 && ball.number !== 8) {
      ctx.save();
      ctx.globalAlpha = 0.07;
      ctx.beginPath();
      ctx.ellipse(0, r * 0.88, r * 0.45, r * 0.10, 0, 0, Math.PI * 2);
      ctx.fillStyle = ballColor(ball.number);
      ctx.fill();
      ctx.restore();
    }
  }

  // Drop shadow
  ctx.shadowColor = "rgba(0,0,0,0.36)";
  ctx.shadowBlur = isFullSize ? 7 : 4 * scale;
  ctx.shadowOffsetX = 0.5 * scale;
  ctx.shadowOffsetY = 3 * scale;

  if (ball.number === 0) {
    // Cue ball — pre-rendered sprite
    const sprite = getCueBallSprite();
    ctx.drawImage(sprite, -r, -r, r * 2, r * 2);
  } else {
    // Numbered ball — atlas sprite
    const atlas = buildBallAtlas(ball.number);
    const rollPhase = spin?.rollPhase ?? 0;
    const heading = spin?.lastHeading ?? 0;
    const TWO_PI = Math.PI * 2;
    const norm = ((rollPhase % TWO_PI) + TWO_PI) % TWO_PI;
    const framePos = (norm / TWO_PI) * SPRITE_FRAMES;
    const baseIdx = Math.floor(framePos) % SPRITE_FRAMES;
    const nextIdx = (baseIdx + 1) % SPRITE_FRAMES;
    const frameBlend = framePos - Math.floor(framePos);

    ctx.save();
    ctx.rotate(heading - Math.PI * 0.5);
    if (frameBlend > 0.001) {
      ctx.globalAlpha = 1 - frameBlend;
      ctx.drawImage(atlas, baseIdx * SPRITE_PX, 0, SPRITE_PX, SPRITE_PX, -r, -r, r * 2, r * 2);
      ctx.globalAlpha = frameBlend;
      ctx.drawImage(atlas, nextIdx * SPRITE_PX, 0, SPRITE_PX, SPRITE_PX, -r, -r, r * 2, r * 2);
      ctx.globalAlpha = 1;
    } else {
      ctx.drawImage(atlas, baseIdx * SPRITE_PX, 0, SPRITE_PX, SPRITE_PX, -r, -r, r * 2, r * 2);
    }
    ctx.restore();
  }

  ctx.shadowColor = "transparent";
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

  // Pocket depth glow — dark gradient inside each pocket for 3D depth
  for (const pocket of POCKETS) {
    const pg = ctx.createRadialGradient(pocket.x, pocket.y, 2, pocket.x, pocket.y, 22);
    pg.addColorStop(0, "rgba(0,0,0,0.65)");
    pg.addColorStop(0.5, "rgba(0,0,0,0.25)");
    pg.addColorStop(1, "rgba(0,0,0,0)");
    ctx.beginPath();
    ctx.arc(pocket.x, pocket.y, 22, 0, Math.PI * 2);
    ctx.fillStyle = pg;
    ctx.fill();
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
    drawCue(ctx, cueBall, aimAngle, pullRatio, cueSprite, AIM_RENDER_METRICS);
  }

  // STEP 2: All balls
  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    drawBall(ctx, ball, 1, ballSpinCache.get(ball.id));
  }

  // STEP 3: Remote overlay AFTER balls so the transparent cue/mira stay visible.
  if (remoteOverlay) {
    drawRemoteAimOverlay(ctx, cueSprite, remoteOverlay.cueBall, remoteOverlay.aimAngle, remoteOverlay.preview, remoteOverlay.pullRatio, remoteOverlay.mode, AIM_RENDER_METRICS, (overlayCtx, ball, alpha) => drawBall(overlayCtx, ball, alpha));
  }

  // STEP 4: Ghost ball circle AFTER balls (so it's visible on top)
  if (cueBall && showGuide && preview) {
    drawGhostBall(ctx, cueBall, preview, previewPowerRatio, illegalTarget, AIM_RENDER_METRICS);
  }


  // Pocket animations
  for (const anim of pocketAnimations) {
    drawPocketAnimation(ctx, anim, now, ballSpinCache.get(anim.ball.id));
  }

  // Ball-in-hand indicator — pulsing glow
  if (cueBall && isBallInHand) {
    const pulse = 0.5 + Math.sin(now * 0.005) * 0.3;
    ctx.save();
    ctx.globalAlpha = pulse;
    const bihGrad = ctx.createRadialGradient(cueBall.x, cueBall.y, BALL_VISUAL_RADIUS, cueBall.x, cueBall.y, BALL_VISUAL_RADIUS + 14);
    bihGrad.addColorStop(0, "rgba(100,200,255,0.5)");
    bihGrad.addColorStop(0.5, "rgba(100,200,255,0.15)");
    bihGrad.addColorStop(1, "rgba(100,200,255,0)");
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, BALL_VISUAL_RADIUS + 14, 0, Math.PI * 2);
    ctx.fillStyle = bihGrad;
    ctx.fill();
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
  const pendingShotAdvanceRef = useRef<{ advance: number; lastAt: number } | null>(null);
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

  // Pre-render ball sprite atlases (runs once synchronously before first draw)
  useMemo(() => { if (typeof document !== 'undefined') ensureAllAtlases(); }, []);

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
      // Keep authoritative snapshots untouched here. Cue-ball optimism is handled
      // in a single place during the draw step to avoid one-frame rollback fights
      // between queue ingestion and visual override.
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
      pendingShotAdvanceRef.current = null;
      return;
    }
    const progress = computePendingShotProgress(pending, performance.now());
    const absoluteExpiryMs = PENDING_SHOT_VISUAL_MAX_MS + PENDING_SHOT_POST_IMPACT_HOLD_MS + 260;
    if (progress.elapsed >= absoluteExpiryMs && game.status !== "simulating") {
      pendingShotVisualRef.current = null;
      pendingShotAdvanceRef.current = null;
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
    const authoritativeNearExpected = authoritativeDistanceFromExpected <= Math.max(10, BALL_RADIUS * 1.05);
    const authoritativeClearlyStarted = authoritativeDistanceFromStart > Math.max(2.2, BALL_RADIUS * 0.4);
    const authoritativeMadeMeaningfulProgress = authoritativeDistanceFromStart > Math.max(7, pending.travelLimit * 0.18);
    const shotLockActive = progress.elapsed < CUE_SHOT_START_LOCK_MS;
    const hardExpiryDuringSimMs = absoluteExpiryMs + 360;
    if (!shotLockActive && (authoritativeNearExpected || (authoritativeClearlyStarted && (authoritativeMadeMeaningfulProgress || revisionAdvanced || shotSequenceAdvanced)) || progress.elapsed >= hardExpiryDuringSimMs)) {
      pendingShotVisualRef.current = null;
      pendingShotAdvanceRef.current = null;
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
    const settledCueX = placedCue?.x ?? latestCueBall?.x ?? cueBall?.x ?? null;
    const settledCueY = placedCue?.y ?? latestCueBall?.y ?? cueBall?.y ?? null;
    const settledRemoteMode: AimPointerMode = previousMode === "place" ? "place" : "aim";
    emitAimState({
      visible: true,
      angle: aimAngleRef.current,
      cueX: settledCueX,
      cueY: settledCueY,
      mode: settledRemoteMode,
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
    pendingShotAdvanceRef.current = null;
    if (liveCueBall) {
      const livePreview = computeAimPreview(liveCueBall, state.renderBalls, aimAngleRef.current);
      const travelLimit = livePreview.contactX !== null && livePreview.contactY !== null
        ? Math.max(20, Math.hypot(livePreview.contactX - liveCueBall.x, livePreview.contactY - liveCueBall.y))
        : Math.max(24, Math.hypot(livePreview.endX - liveCueBall.x, livePreview.endY - liveCueBall.y));
      const estimatedSpeedPxPerMs = estimateCueVisualSpeedPxPerMs(shotPower);
      const impactAtMs = travelLimit > 1 ? clamp(travelLimit / estimatedSpeedPxPerMs, 22, 240) : null;
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
      pendingShotAdvanceRef.current = { advance: 0, lastAt: shotStartedAt };
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
            smoothedBalls = interpolateSnapshotEntries(fromSnapshot, toSnapshot, eased);
          } else {
            const overshootMs = Math.max(0, renderServerTime - toSnapshot.serverAt);
            smoothedBalls = extrapolateSnapshotBalls(toSnapshot.balls, toSnapshot.velocities, Math.min(overshootMs, REALTIME_MAX_EXTRAPOLATION_MS));
          }
          if (pendingShotVisualRef.current) {
            const visualCue = realtimeVisualBallsRef.current.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
            if (visualCue) {
              smoothedBalls = smoothedBalls.map((ball) => (ball.number === 0 ? { ...ball, x: visualCue.x, y: visualCue.y, pocketed: false } : ball));
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
          let smoothedBalls = extrapolated;
          if (pendingShotVisualRef.current) {
            const visualCue = realtimeVisualBallsRef.current.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
            if (visualCue) {
              smoothedBalls = smoothedBalls.map((ball) => (ball.number === 0 ? { ...ball, x: visualCue.x, y: visualCue.y, pocketed: false } : ball));
            }
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
      const remoteHasCuePreview = Boolean(
        remoteAimFresh
        && remoteAimState
        && remoteAimState.cueX !== null
        && remoteAimState.cueY !== null
        && remoteMode !== "idle"
      );
      const remoteCueSource = remoteCanRender || remoteCuePlacementActive || remoteHasCuePreview
        ? {
            id: "ball-0-remote-overlay",
            number: 0,
            x: remoteHasCuePreview
              ? (remoteAimState?.cueX ?? authoritativeRemoteCue.x)
              : authoritativeRemoteCue.x,
            y: remoteHasCuePreview
              ? (remoteAimState?.cueY ?? authoritativeRemoteCue.y)
              : authoritativeRemoteCue.y,
            pocketed: false,
          }
        : null;
      const remoteVisual = remoteAimVisualRef.current;
      let remoteCueBall: GameBallSnapshot | null = null;
      let remoteAimAngle = 0;
      let remotePullRatio = 0.05;

      if ((remoteCanRender || remoteCuePlacementActive || remoteHasCuePreview) && remoteAimState && remoteCueSource) {
        const targetX = remoteHasCuePreview
          ? (remoteAimState.cueX ?? remoteCueSource.x)
          : remoteCueSource.x;
        const targetY = remoteHasCuePreview
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
          const posLerp = remoteHasCuePreview ? 0.78 : 0.42;
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

      if (remoteCueBall && (remoteCuePlacementActive || remoteHasCuePreview)) {
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
        remoteAimFresh
        && remoteAimState
        && remoteCueBall
        && (remoteCanRender || remoteCuePlacementActive || remoteHasCuePreview)
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
          pendingShotAdvanceRef.current = null;
        } else {
          const progress = computePendingShotProgress(pendingShotVisual, now);
          const heldElapsed = progress.heldElapsed;
          const travelMs = progress.travelMs;
          const moveProgress = progress.moveProgress;
          const shotDirX = Math.cos(pendingShotVisual.angle);
          const shotDirY = Math.sin(pendingShotVisual.angle);
          const authoritativeAdvance = ((drawCueBall.x - pendingShotVisual.cueX) * shotDirX) + ((drawCueBall.y - pendingShotVisual.cueY) * shotDirY);
          const optimisticAdvance = ((progress.expectedCueX - pendingShotVisual.cueX) * shotDirX) + ((progress.expectedCueY - pendingShotVisual.cueY) * shotDirY);
          const advanceState = pendingShotAdvanceRef.current ?? { advance: 0, lastAt: pendingShotVisual.startedAt };
          const deltaMs = clamp(now - advanceState.lastAt, 1, 34);
          const shotStartRamp = smoothstep(0, CUE_SHOT_START_RAMP_MS, heldElapsed);
          const maxLead = lerp(0.12, maxCueVisualLeadPx(pendingShotVisual.power), shotStartRamp);
          const targetAdvance = game.status === "simulating"
            ? Math.max(authoritativeAdvance, Math.min(optimisticAdvance, authoritativeAdvance + maxLead))
            : optimisticAdvance;
          const stepCap = maxCueAdvanceStepPx(pendingShotVisual.power, pendingShotVisual.estimatedSpeedPxPerMs, deltaMs) * lerp(0.42, 1, shotStartRamp);
          const monotonicAdvance = Math.max(advanceState.advance, targetAdvance);
          const noBacktrackAdvance = Math.min(monotonicAdvance, advanceState.advance + stepCap);
          pendingShotAdvanceRef.current = { advance: noBacktrackAdvance, lastAt: now };
          const optimisticCue = {
            ...drawCueBall,
            x: pendingShotVisual.cueX + shotDirX * noBacktrackAdvance,
            y: pendingShotVisual.cueY + shotDirY * noBacktrackAdvance,
            pocketed: false,
          };
          drawBalls = drawBalls.map((ball) => (ball.number === 0 ? optimisticCue : ball));
          drawCueBall = optimisticCue;
          // Sync visual ref so next frame lerp starts from the forward-most cue position,
          // preventing the quick rollback/return snap right after shot start.
          realtimeVisualBallsRef.current = realtimeVisualBallsRef.current.map(
            b => b.number === 0 ? { ...b, x: optimisticCue.x, y: optimisticCue.y } : b
          );
          const spin = ballSpinRef.current.get(drawCueBall.id);
          if (spin) {
            spin.lastX = optimisticCue.x;
            spin.lastY = optimisticCue.y;
            spin.lastSeenAt = now;
          }
          if (pendingShotVisual.impactType && !pendingShotVisual.firstImpactPlayed && pendingShotVisual.impactAtMs !== null && heldElapsed >= pendingShotVisual.impactAtMs) {
            pendingShotVisual.firstImpactPlayed = true;
          }
          if (moveProgress >= 1 && heldElapsed >= travelMs + PENDING_SHOT_POST_IMPACT_HOLD_MS && game.status !== "simulating") {
            pendingShotVisualRef.current = null;
            pendingShotAdvanceRef.current = null;
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

  const railEntries = railBalls.map((entry: RailBallAnimation) => ({
    entry,
    src: ballIconCache.get(entry.number) ?? null,
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
              {railEntries.map(({ entry, src }: { entry: RailBallAnimation; src: string | null }) => (
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
