import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type TouchEvent as ReactTouchEvent } from "react";
import type { AimPointerMode, AimStateSnapshot, BallGroup, GameBallSnapshot, GameShotFrame, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
import tableAsset from "../assets/game/pool-table-public.png";
import cueAsset from "../assets/game/pool-cue-public.png";

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
const BALL_VISUAL_RADIUS = 15; // Draw slightly larger than physics for visibility (#8)
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

const POCKET_ANIM_DURATION = 320;
const POWER_MIN = 0.0009;
const POWER_RETURN_MS = 180;
const POWER_CURVE_EXPONENT = 5.1;
const AIM_SYNC_INTERVAL_MS = 22;
const PLACE_SYNC_INTERVAL_MS = 16;
const REMOTE_AIM_STALE_MS = 12000;
const REALTIME_VISUAL_SNAP_DISTANCE = 42;
const REALTIME_SNAPSHOT_QUEUE_LIMIT = 8;
const REALTIME_RENDER_DELAY_MS = 50;

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
  onAimStateChange?: (aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; power?: number | null; seq?: number; mode: AimPointerMode }) => void;
  onExit: () => void;
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
    const dx = ball.x - prev.x;
    const dy = ball.y - prev.y;
    const dist = Math.hypot(dx, dy);
    if (dist >= REALTIME_VISUAL_SNAP_DISTANCE) return { ...ball };
    return {
      ...ball,
      x: lerp(prev.x, ball.x, alpha),
      y: lerp(prev.y, ball.y, alpha),
      pocketed: (alpha < 0.5 ? prev.pocketed : ball.pocketed),
    };
  });
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
    const distance = Math.hypot(dx, dy);
    const targetAxis = distance > 0.01 ? Math.atan2(dy, dx) + Math.PI / 2 : current.axis;
    current.axis = lerpAngle(current.axis, targetAxis, distance > 0.08 ? 0.5 : 0.18);

    if (distance > 0.01) {
      const measuredPhaseVelocity = distance / (BALL_VISUAL_RADIUS * 1.04 * elapsedFrames);
      current.phaseVelocity = lerp(current.phaseVelocity, measuredPhaseVelocity, 0.62);
      current.visualSpeed = lerp(current.visualSpeed, distance / elapsedFrames, 0.56);
      current.lastX = ball.x;
      current.lastY = ball.y;
    } else {
      current.phaseVelocity *= 0.92;
      current.visualSpeed *= 0.9;
    }

    current.phase = (current.phase + current.phaseVelocity * elapsedFrames) % (Math.PI * 2);
    current.labelDepth = Math.cos(current.phase);
    current.lastSeenAt = now;
  }

  for (const [id, spin] of cache) {
    if (!seen.has(id) && now - spin.lastSeenAt > 800) cache.delete(id);
  }
}

function drawStripedWrapBand(
  ctx: CanvasRenderingContext2D,
  r: number,
  color: string,
  phase: number,
  scale: number,
) {
  const innerR = Math.max(1, r - 0.55 * scale);
  const rowStep = Math.max(0.8, 0.9 * scale);
  const sampleStep = Math.max(0.7, 0.9 * scale);
  const bandHalf = 0.34;
  const cosP = Math.cos(phase);
  const sinP = Math.sin(phase);

  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, innerR, 0, Math.PI * 2);
  ctx.clip();

  const stripeGrad = ctx.createLinearGradient(-innerR, 0, innerR, 0);
  stripeGrad.addColorStop(0, shadeColor(color, -18));
  stripeGrad.addColorStop(0.22, color);
  stripeGrad.addColorStop(0.5, shadeColor(color, 10));
  stripeGrad.addColorStop(0.78, color);
  stripeGrad.addColorStop(1, shadeColor(color, -20));
  ctx.fillStyle = stripeGrad;

  for (let py = -innerR; py <= innerR; py += rowStep) {
    const xSpan = Math.sqrt(Math.max(0, innerR * innerR - py * py));
    let segmentStart: number | null = null;
    let lastInsideX = -xSpan;
    for (let px = -xSpan; px <= xSpan + sampleStep * 0.5; px += sampleStep) {
      const nx = clamp(px / innerR, -1, 1);
      const ny = clamp(py / innerR, -1, 1);
      const nzSq = 1 - nx * nx - ny * ny;
      const nz = nzSq > 0 ? Math.sqrt(nzSq) : 0;
      const localY = ny * cosP - nz * sinP;
      const insideStripe = Math.abs(localY) <= bandHalf;
      if (insideStripe && segmentStart === null) segmentStart = px;
      if (insideStripe) lastInsideX = px;
      const shouldFlush = (!insideStripe && segmentStart !== null) || (segmentStart !== null && px >= xSpan);
      if (shouldFlush && segmentStart !== null) {
        const endX = insideStripe ? px : lastInsideX + sampleStep;
        ctx.fillRect(segmentStart, py - rowStep * 0.54, Math.max(sampleStep, endX - segmentStart), rowStep * 1.08);
        segmentStart = null;
      }
    }
  }

  ctx.restore();

  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, innerR, 0, Math.PI * 2);
  ctx.clip();
  ctx.globalAlpha = 0.28;
  ctx.strokeStyle = "rgba(18, 24, 36, 0.34)";
  ctx.lineWidth = Math.max(0.8, 1.05 * scale);
  const stripeEdge = innerR * (0.34 + 0.08 * Math.abs(Math.cos(phase)));
  const stripeCenter = -Math.sin(phase) * innerR * 0.36;
  ctx.beginPath();
  ctx.ellipse(0, stripeCenter - stripeEdge, innerR * 0.9, innerR * 0.12, 0, 0, Math.PI * 2);
  ctx.ellipse(0, stripeCenter + stripeEdge, innerR * 0.9, innerR * 0.12, 0, 0, Math.PI * 2);
  ctx.stroke();
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

  ctx.shadowColor = "rgba(0, 0, 0, 0.50)";
  ctx.shadowBlur = 10 * scale;
  ctx.shadowOffsetX = 1 * scale;
  ctx.shadowOffsetY = 5 * scale;

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

    if (isStripe) {
      drawStripedWrapBand(ctx, r, color, spinPhase, scale);
    }

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
  const eased = t * t;
  const scale = lerp(1, 0.08, eased);
  const alpha = lerp(1, 0, Math.pow(t, 0.7));
  if (alpha <= 0.02) return;
  ctx.save();
  ctx.globalAlpha = alpha;
  const ball = { ...anim.ball, x: lerp(anim.ball.x, anim.pocketX, eased), y: lerp(anim.ball.y, anim.pocketY, eased) };
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
  const cueGap = BALL_RADIUS + 24 + pullRatio * 118;
  const cueLength = 1040;
  const drawHeight = cueSprite.complete && cueSprite.naturalWidth
    ? Math.max(10, cueLength * (cueSprite.naturalHeight / cueSprite.naturalWidth))
    : 10;

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
    ctx.lineWidth = 7;
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

  ctx.save();
  ctx.globalAlpha = mode === "place" ? 0.78 : 0.5;
  drawBall(ctx, cueBall, 1);
  ctx.restore();

  if (showGuide) {
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

  if (showPlacementRing) {
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

  ctx.save();
  ctx.globalAlpha = mode === "power" ? 0.64 : mode === "aim" ? 0.56 : 0.48;
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

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, opponentAim, onShoot, onAimStateChange, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
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
  const ballSpinRef = useRef<Map<string, BallSpinState>>(new Map());
  const snapAnimRef = useRef<{ startedAt: number; power: number; fired: boolean } | null>(null);
  const pendingShotVisualRef = useRef<{ startedAt: number; angle: number; power: number; cueX: number; cueY: number; travelLimit: number; estimatedSpeedPxPerMs: number; impactType: "ball" | "cushion" | null; impactAtMs: number | null; firstImpactPlayed: boolean } | null>(null);
  const queuedSfxRef = useRef<number[]>([]);
  const playbackSoundStateRef = useRef<{ seq: number; frameIndex: number; lastBallAt: number; lastCushionAt: number }>({ seq: 0, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0 });
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
  const snapshotQueueRef = useRef<Array<{
    balls: GameBallSnapshot[];
    revision: number;
    receivedAt: number;
  }>>([{
    balls: game.balls.map((ball) => ({ ...ball })),
    revision: game.snapshotRevision ?? 0,
    receivedAt: performance.now(),
  }]);
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
    let nextBalls = game.balls;
    if (game.ballInHandUserId === currentUserId && localCuePlacementRef.current) {
      const placed = clampCuePosition(
        localCuePlacementRef.current.x,
        localCuePlacementRef.current.y,
        game.shotSequence === 0,
      );
      nextBalls = game.balls.map((ball) => (ball.number === 0 ? { ...ball, x: placed.x, y: placed.y, pocketed: false } : ball));
    }
    setDisplayBalls(nextBalls);
  }, [animating, currentUserId, game.ballInHandUserId, game.balls, game.shotSequence]);

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
      }];
      return;
    }

    const nextRevision = game.snapshotRevision ?? 0;
    const queue = snapshotQueueRef.current;
    const lastRevision = queue.length ? queue[queue.length - 1].revision : -1;
    if (nextRevision <= lastRevision) return;

    if (!queue.length) {
      queue.push({
        balls: (realtimeVisualBallsRef.current.length ? realtimeVisualBallsRef.current : displayBalls).map((ball) => ({ ...ball })),
        revision: Math.max(0, nextRevision - 1),
        receivedAt: now - REALTIME_RENDER_DELAY_MS,
      });
    }

    queue.push({
      balls: displayBalls.map((ball) => ({ ...ball })),
      revision: nextRevision,
      receivedAt: now,
    });

    if (queue.length > REALTIME_SNAPSHOT_QUEUE_LIMIT) {
      queue.splice(0, queue.length - REALTIME_SNAPSHOT_QUEUE_LIMIT);
    }
  }, [animating, displayBalls, game.snapshotRevision, game.status]);

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
    playbackSoundStateRef.current = { seq: game.lastShot.seq, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0 };
  }, [animating, game]);

  useEffect(() => () => {
    if (drawLoopRef.current !== null) window.cancelAnimationFrame(drawLoopRef.current);
    if (powerReturnAnimRef.current !== null) window.cancelAnimationFrame(powerReturnAnimRef.current);
    clearQueuedSfx(queuedSfxRef);
    onAimStateChangeRef.current?.({ visible: false, angle: aimAngleRef.current, cueX: null, cueY: null, mode: "idle" });
  }, []);

  const renderBalls = useMemo(() => {
    const visibleNonCue = displayBalls.filter((ball) => !ball.pocketed && ball.number !== 0);
    if ((game.phase === "break" || game.shotSequence === 0) && visibleNonCue.length < 15) {
      return buildOpeningBalls(displayBalls);
    }
    if (game.ballInHandUserId) {
      const cue = displayBalls.find((ball) => ball.number === 0) ?? null;
      if (!cue || cue.pocketed) {
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
    if (liveCueBall) {
      const livePreview = computeAimPreview(liveCueBall, state.renderBalls, aimAngleRef.current);
      const travelLimit = livePreview.contactX !== null && livePreview.contactY !== null
        ? Math.max(20, Math.hypot(livePreview.contactX - liveCueBall.x, livePreview.contactY - liveCueBall.y))
        : Math.max(24, Math.hypot(livePreview.endX - liveCueBall.x, livePreview.endY - liveCueBall.y));
      const estimatedSpeedPxPerMs = lerp(0.95, 4.85, Math.pow(shotPower, 0.58));
      const impactAtMs = travelLimit > 1 ? clamp(travelLimit / estimatedSpeedPxPerMs, 18, 210) : null;
      pendingShotVisualRef.current = {
        startedAt: shotStartedAt,
        angle: aimAngleRef.current,
        power: shotPower,
        cueX: liveCueBall.x,
        cueY: liveCueBall.y,
        travelLimit,
        estimatedSpeedPxPerMs,
        impactType: livePreview.hitBall ? "ball" : "cushion",
        impactAtMs,
        firstImpactPlayed: false,
      };
      SFX.cueHit(shotPower);
      if (impactAtMs !== null) {
        queueSfx(queuedSfxRef, impactAtMs, () => {
          const pending = pendingShotVisualRef.current;
          if (!pending || pending.startedAt !== shotStartedAt || pending.firstImpactPlayed) return;
          pending.firstImpactPlayed = true;
          if (pending.impactType === "ball") SFX.ballHit();
          else if (pending.impactType === "cushion") SFX.cushion();
        });
      }
    }
    setPointerModeSafe("idle");
    emitAimState({ visible: false, angle: aimAngleRef.current, cueX: liveCueBall?.x ?? null, cueY: liveCueBall?.y ?? null, mode: "idle" }, true);

    if (!liveCueBall || !canInteractRef.current || shootBusyRef.current) {
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

    if (state.isBallInHand) {
      localCuePlacementRef.current = { x: liveCueBall.x, y: liveCueBall.y };
    }

    // Fire the real shot immediately, then let visual/audio reactions happen in parallel.
    void onShootRef.current(payload).catch(() => {});
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
          const nearFinalFrame = frameIndex >= Math.max(0, playback.frames.length - 3);
          if (nearFinalFrame && ballsNearlyMatchFrame(drawBalls, playback.frames[playback.frames.length - 1].balls, 0.16)) {
            settlePlayback();
          }
        }
      }

      if ((!playback || !playback.frames.length) && game.status === "simulating" && drawBalls.length) {
        const queue = snapshotQueueRef.current;
        if (queue.length >= 2) {
          const renderTime = now - REALTIME_RENDER_DELAY_MS;
          while (queue.length >= 3 && queue[1].receivedAt <= renderTime) {
            queue.shift();
          }
          const fromSnapshot = queue[0];
          const toSnapshot = queue[1] ?? queue[0];
          const span = Math.max(1, toSnapshot.receivedAt - fromSnapshot.receivedAt);
          const t = clamp((renderTime - fromSnapshot.receivedAt) / span, 0, 1);
          const eased = t * t * (3 - 2 * t);
          const smoothedBalls = interpolateSnapshotBalls(fromSnapshot.balls, toSnapshot.balls, eased);
          realtimeVisualBallsRef.current = smoothedBalls.map((ball) => ({ ...ball }));
          realtimeVisualLastAtRef.current = now;
          drawBalls = smoothedBalls;
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
        } else if (queue.length === 1) {
          const copied = queue[0].balls.map((ball) => ({ ...ball }));
          realtimeVisualBallsRef.current = copied;
          realtimeVisualLastAtRef.current = now;
          drawBalls = copied;
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
        }
      } else if (!playback || !playback.frames.length) {
        const copied = drawBalls.map((ball) => ({ ...ball }));
        realtimeVisualBallsRef.current = copied;
        realtimeVisualLastAtRef.current = now;
        snapshotQueueRef.current = [{
          balls: copied.map((ball) => ({ ...ball })),
          revision: game.snapshotRevision ?? 0,
          receivedAt: now,
        }];
      }

      // Detect newly pocketed balls → trigger animations
      const currentPocketedIds = new Set<string>();
      for (const ball of drawBalls) { if (ball.pocketed) currentPocketedIds.add(ball.id); }
      for (const ball of drawBalls) {
        if (ball.pocketed && !prevPocketedIdsRef.current.has(ball.id)) {
          let closestPocket: typeof POCKETS[number] = POCKETS[0];
          let minDist = Infinity;
          for (const pocket of POCKETS) {
            const d = Math.hypot(pocket.x - ball.x, pocket.y - ball.y);
            if (d < minDist) { minDist = d; closestPocket = pocket; }
          }
          pocketAnimationsRef.current.push({ ball, pocketX: closestPocket.x, pocketY: closestPocket.y, startedAt: now });
          SFX.pocket();
        }
      }
      prevPocketedIdsRef.current = currentPocketedIds;
      pocketAnimationsRef.current = pocketAnimationsRef.current.filter((anim) => now - anim.startedAt < POCKET_ANIM_DURATION);

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
          playbackSoundStateRef.current = { seq: playback.seq, frameIndex: -1, lastBallAt: 0, lastCushionAt: 0 };
        }
        if (soundFrameIndex > playbackSoundStateRef.current.frameIndex) {
          const prevFrame = playback.frames[Math.max(0, soundFrameIndex - 1)]?.balls ?? [];
          const currFrame = playback.frames[soundFrameIndex]?.balls ?? [];
          const prevMap = new Map(prevFrame.map((ball) => [ball.id, ball]));
          let newlyPocketed = false;
          let movingNonCue = 0;
          let wallTouch = false;
          for (const ball of currFrame) {
            const prevBall = prevMap.get(ball.id);
            if (!prevBall) continue;
            if (ball.pocketed && !prevBall.pocketed) newlyPocketed = true;
            const moved = Math.hypot(ball.x - prevBall.x, ball.y - prevBall.y);
            if (!ball.pocketed && ball.id !== "ball-0" && moved > 0.8) movingNonCue += 1;
            if (!ball.pocketed && moved > 0.55) {
              if (ball.x <= PLAY_MIN_X + 2.4 || ball.x >= PLAY_MAX_X - 2.4 || ball.y <= PLAY_MIN_Y + 2.4 || ball.y >= PLAY_MAX_Y - 2.4) {
                wallTouch = true;
              }
            }
          }
          const stateNow = playbackSoundStateRef.current;
          if (movingNonCue > 0 && now - stateNow.lastBallAt > 72) {
            SFX.ballHit();
            stateNow.lastBallAt = now;
          } else if (wallTouch && now - stateNow.lastCushionAt > 84 && !newlyPocketed) {
            SFX.cushion();
            stateNow.lastCushionAt = now;
          }
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
        && game.status !== "finished"
      );
      const fallbackRemoteCueX = remoteAimState?.cueX ?? drawCueBall?.x ?? cueBall?.x ?? DEFAULT_CUE_X;
      const fallbackRemoteCueY = remoteAimState?.cueY ?? drawCueBall?.y ?? cueBall?.y ?? DEFAULT_CUE_Y;
      const remoteCueSource = remoteCanRender
        ? {
            id: "ball-0-remote-overlay",
            number: 0,
            x: fallbackRemoteCueX,
            y: fallbackRemoteCueY,
            pocketed: false,
          }
        : null;
      const remoteVisual = remoteAimVisualRef.current;
      let remoteCueBall: GameBallSnapshot | null = null;
      let remoteAimAngle = 0;
      let remotePullRatio = 0.05;

      if (remoteCanRender && remoteAimState && remoteCueSource) {
        const targetX = remoteAimState.cueX ?? remoteCueSource.x;
        const targetY = remoteAimState.cueY ?? remoteCueSource.y;
        const targetAngle = remoteAimState.angle;
        const targetPull = clamp(remoteAimState.power ?? 0, 0, 1);
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
          const posLerp = remoteMode === "place" ? 0.72 : 0.42;
          const angleLerp = remoteMode === "aim" ? 0.36 : remoteMode === "power" ? 0.3 : 0.2;
          const pullLerp = remoteMode === "power" ? 0.5 : 0.26;
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
        remotePullRatio = remoteMode === "power"
          ? clamp(0.2 + remoteVisual.pull * 0.8, 0.2, 0.98)
          : remoteMode === "aim"
            ? 0.1
            : remoteMode === "place"
              ? 0.08
              : 0.05;
      } else if (!remoteAimFresh || remoteMode === "idle") {
        remoteVisual.initialized = false;
      }

      const remoteCueMoved = Boolean(
        remoteCueBall
        && drawCueBall
        && (Math.abs(remoteCueBall.x - drawCueBall.x) > 0.15 || Math.abs(remoteCueBall.y - drawCueBall.y) > 0.15)
      );
      if (remoteCueBall && (game.ballInHandUserId === remoteAimState?.userId || remoteMode === "place" || remoteCueMoved)) {
        let replacedCue = false;
        drawBalls = drawBalls.map((ball) => {
          if (ball.number !== 0) return ball;
          replacedCue = true;
          return { ...ball, x: remoteCueBall!.x, y: remoteCueBall!.y, pocketed: false };
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
      const shotKick = !animating ? pendingShotVisualRef.current : null;
      if (shotKick && drawCueBall) {
        const kickElapsed = now - shotKick.startedAt;
        const KICK_MS = 240;
        const eventTime = shotKick.impactAtMs ?? KICK_MS;
        const activeMs = Math.min(KICK_MS, Math.max(eventTime + 36, 96));
        if (kickElapsed >= activeMs) {
          pendingShotVisualRef.current = null;
        } else {
          const kickDistanceRaw = kickElapsed * shotKick.estimatedSpeedPxPerMs;
          const kickDistance = Math.min(shotKick.travelLimit, kickDistanceRaw);
          const easedRatio = shotKick.travelLimit > 0 ? clamp(kickDistance / shotKick.travelLimit, 0, 1) : 0;
          const easedKick = 1 - Math.pow(1 - easedRatio, 2.2);
          const finalDistance = shotKick.travelLimit * easedKick;
          const kickX = shotKick.cueX + Math.cos(shotKick.angle) * finalDistance;
          const kickY = shotKick.cueY + Math.sin(shotKick.angle) * finalDistance;
          let cueReplaced = false;
          drawBalls = drawBalls.map((ball) => {
            if (ball.number !== 0 || ball.pocketed) return ball;
            cueReplaced = true;
            return { ...ball, x: kickX, y: kickY, pocketed: false };
          });
          drawCueBall = cueReplaced
            ? drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? drawCueBall
            : { ...drawCueBall, x: kickX, y: kickY, pocketed: false };
        }
      } else if (animating) {
        pendingShotVisualRef.current = null;
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

        <div className={`pool-stage__player ${game.turnUserId === leftPlayer?.userId ? "pool-stage__player--active" : ""}`}>
          <div className="pool-stage__avatar">
            {leftPlayer?.avatarUrl ? <img src={leftPlayer.avatarUrl} alt={cleanName(leftPlayer.displayName)} /> : <span>{playerInitials(leftPlayer)}</span>}
          </div>
          <div className="pool-stage__player-copy">
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
        </div>

        <div className="pool-stage__status">
          <span className="pool-stage__stake">{cueLabel}</span>
          <strong>{statusText}</strong>
          <small>{phaseText}</small>
        </div>

        <div className={`pool-stage__player pool-stage__player--right ${game.turnUserId === rightPlayer?.userId ? "pool-stage__player--active" : ""}`}>
          <div className="pool-stage__player-copy pool-stage__player-copy--right">
            <strong>{cleanName(rightPlayer?.displayName ?? "Adversário")}</strong>
            <div className="pool-stage__pips pool-stage__pips--right">
              {Array.from({ length: 7 }).map((_, index) => {
                const number = rightPocketed[index] ?? null;
                return number !== null
                  ? <BallPip key={`right-${index}`} number={number} />
                  : <span key={`right-${index}`} className="pool-stage__pip" />;
              })}
            </div>
          </div>
          <div className="pool-stage__avatar">
            {rightPlayer?.avatarUrl ? <img src={rightPlayer.avatarUrl} alt={cleanName(rightPlayer.displayName)} /> : <span>{playerInitials(rightPlayer)}</span>}
          </div>
        </div>
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
            <div className="pool-stage__power-gradient" />
            <div className="pool-stage__power-guides" />
            <div className="pool-stage__power-cue" style={{ top: `calc(${(displayedPowerCueTop * 100).toFixed(1)}% + 4px)` }}>
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
      </div>
    </section>
  );
}
