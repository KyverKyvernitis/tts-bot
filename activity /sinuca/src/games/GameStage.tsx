import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { AimPointerMode, AimStateSnapshot, BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
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
const POWER_MIN = 0.06;
const POWER_RETURN_MS = 180;
const POWER_DEADZONE_PX = 14;
const POWER_FULL_TRAVEL_RATIO = 0.9;
const POWER_CURVE_EXPONENT = 1.75;
const AIM_SYNC_INTERVAL_MS = 44;

type ShotInput = {
  angle: number;
  power: number;
  cueX?: number | null;
  cueY?: number | null;
  calledPocket?: number | null;
};

type Props = {
  room: RoomSnapshot;
  game: GameSnapshot;
  currentUserId: string;
  shootBusy: boolean;
  exitBusy: boolean;
  opponentAim: AimStateSnapshot | null;
  onShoot: (shot: ShotInput) => Promise<void>;
  onAimStateChange?: (aim: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; mode: AimPointerMode }) => void;
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
      cache.set(ball.id, { phase: 0, axis: 0, lastX: ball.x, lastY: ball.y, lastSeenAt: now });
      continue;
    }

    const dx = ball.x - current.lastX;
    const dy = ball.y - current.lastY;
    const distance = Math.hypot(dx, dy);
    if (distance > 0.02) {
      current.phase = (current.phase + distance / (BALL_VISUAL_RADIUS * 1.45)) % (Math.PI * 2);
      current.axis = Math.atan2(dy, dx) + Math.PI / 2;
      current.lastX = ball.x;
      current.lastY = ball.y;
    }
    current.lastSeenAt = now;
  }

  for (const [id, spin] of cache) {
    if (!seen.has(id) && now - spin.lastSeenAt > 800) cache.delete(id);
  }
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
    targetGuideX = hitBall.x + nnx * 66;
    targetGuideY = hitBall.y + nny * 66;
    const dotN = dx * nnx + dy * nny;
    const remVx = dx - dotN * nnx;
    const remVy = dy - dotN * nny;
    const remLen = Math.hypot(remVx, remVy);
    const DEFLECT_DIST = 100;
    if (remLen > 0.05) {
      cueDeflectX = contactX + (remVx / remLen) * DEFLECT_DIST;
      cueDeflectY = contactY + (remVy / remLen) * DEFLECT_DIST;
    } else {
      cueDeflectX = contactX;
      cueDeflectY = contactY;
    }
  }

  return { endX: cueBall.x + dx * hitDistance, endY: cueBall.y + dy * hitDistance, hitBall, contactX, contactY, cueDeflectX, cueDeflectY, targetGuideX, targetGuideY };
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

  // Shadow — stronger and more visible (#10)
  ctx.shadowColor = "rgba(0, 0, 0, 0.50)";
  ctx.shadowBlur = 10 * scale;
  ctx.shadowOffsetX = 1 * scale;
  ctx.shadowOffsetY = 5 * scale;

  // Base 3D gradient
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
    const travelY = Math.sin(spinPhase) * r * 0.18;
    const diskScaleY = 0.86 + 0.14 * Math.abs(Math.cos(spinPhase));

    ctx.save();
    ctx.rotate(spinAxis);

    if (ball.number >= 9) {
      const stripeCenterY = Math.sin(spinPhase) * r * 0.18;
      const stripeHalf = r * (0.31 + 0.08 * Math.abs(Math.cos(spinPhase)));
      ctx.save();
      ctx.beginPath();
      ctx.arc(0, 0, r - 0.55 * scale, 0, Math.PI * 2);
      ctx.clip();
      const sg = ctx.createLinearGradient(-r, 0, r, 0);
      sg.addColorStop(0, shadeColor(color, -18));
      sg.addColorStop(0.5, color);
      sg.addColorStop(1, shadeColor(color, -18));
      ctx.fillStyle = sg;
      ctx.fillRect(-r, stripeCenterY - stripeHalf, r * 2, stripeHalf * 2);
      ctx.restore();

      ctx.save();
      ctx.strokeStyle = "rgba(18,26,38,0.34)";
      ctx.lineWidth = 1.1 * scale;
      ctx.beginPath();
      ctx.moveTo(-r * 0.9, stripeCenterY - stripeHalf);
      ctx.lineTo(r * 0.9, stripeCenterY - stripeHalf);
      ctx.moveTo(-r * 0.9, stripeCenterY + stripeHalf);
      ctx.lineTo(r * 0.9, stripeCenterY + stripeHalf);
      ctx.stroke();
      ctx.restore();
    }

    const diskR = r * (ball.number >= 9 ? 0.39 : 0.43);
    const diskGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, diskR);
    diskGrad.addColorStop(0, "#ffffff");
    diskGrad.addColorStop(1, "#e8eef4");
    ctx.save();
    ctx.translate(0, travelY);
    ctx.scale(1, diskScaleY);
    ctx.globalAlpha = 0.8 + 0.2 * diskScaleY;
    ctx.beginPath();
    ctx.arc(0, 0, diskR, 0, Math.PI * 2);
    ctx.fillStyle = diskGrad;
    ctx.fill();
    ctx.restore();

    const fontSize = clamp(Math.round((ball.number >= 10 ? 7 : 8.5) * scale), 5, 18);
    ctx.save();
    ctx.translate(0, travelY);
    ctx.scale(1, diskScaleY);
    ctx.font = `700 ${fontSize}px Inter, system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = ball.number === 8 ? "#0a0c12" : "#1a1e2a";
    ctx.fillText(String(ball.number), 0, 0.5 * scale);
    ctx.restore();

    ctx.restore();
  }

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
function drawAimLine(ctx: CanvasRenderingContext2D, cueBall: GameBallSnapshot, preview: AimPreview) {
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

  if (hasHit && preview.hitBall && preview.targetGuideX !== null && preview.targetGuideY !== null) {
    ctx.save();
    ctx.strokeStyle = "rgba(245, 250, 255, 0.88)";
    ctx.lineWidth = 2.15;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(preview.hitBall.x, preview.hitBall.y);
    ctx.lineTo(preview.targetGuideX, preview.targetGuideY);
    ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.72)";
    ctx.lineWidth = 2.15;
    ctx.beginPath();
    ctx.arc(preview.hitBall.x, preview.hitBall.y, BALL_VISUAL_RADIUS + 1.5, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

// Ghost ball circle — drawn AFTER balls so it's visible on top
function drawGhostBall(ctx: CanvasRenderingContext2D, preview: AimPreview) {
  const hasHit = preview.contactX !== null && preview.contactY !== null && preview.hitBall;
  if (!hasHit) return;

  const ghostX = preview.contactX!;
  const ghostY = preview.contactY!;

  // Outer glow
  ctx.save();
  ctx.beginPath();
  ctx.arc(ghostX, ghostY, BALL_VISUAL_RADIUS + 2, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(200, 240, 255, 0.25)";
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.restore();

  // Main ghost ball circle — thick and bright
  ctx.save();
  ctx.beginPath();
  ctx.arc(ghostX, ghostY, BALL_VISUAL_RADIUS, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
  ctx.lineWidth = 3.05;
  ctx.stroke();
  ctx.restore();
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
) {
  const lineEndX = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactX : preview?.endX ?? (cueBall.x + Math.cos(aimAngle) * 420);
  const lineEndY = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactY : preview?.endY ?? (cueBall.y + Math.sin(aimAngle) * 420);

  ctx.save();
  ctx.globalAlpha = 0.42;
  ctx.strokeStyle = "rgba(244, 248, 255, 0.86)";
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.globalAlpha = 0.38;
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
  needEightCall: boolean,
  selectedPocket: number | null,
  isBallInHand: boolean,
  pocketAnimations: PocketAnimation[],
  now: number,
  ballSpinCache: Map<string, BallSpinState>,
  remoteOverlay: { cueBall: GameBallSnapshot; aimAngle: number; preview: AimPreview | null; pullRatio: number } | null,
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

  // STEP 1: Remote aim overlay first, then local aim line + cue BEFORE balls.
  if (remoteOverlay) {
    drawRemoteAimOverlay(ctx, cueSprite, remoteOverlay.cueBall, remoteOverlay.aimAngle, remoteOverlay.preview, remoteOverlay.pullRatio);
  }
  if (cueBall && showGuide && preview) {
    drawAimLine(ctx, cueBall, preview);
    drawCue(ctx, cueBall, aimAngle, pullRatio, cueSprite);
  }

  // STEP 2: All balls
  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    drawBall(ctx, ball, 1, ballSpinCache.get(ball.id));
  }

  // STEP 3: Ghost ball circle AFTER balls (so it's visible on top)
  if (cueBall && showGuide && preview) {
    drawGhostBall(ctx, preview);
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
  const powerGestureRef = useRef<{ startY: number; fullTravelPx: number } | null>(null);
  const localCuePlacementRef = useRef<{ x: number; y: number } | null>(null);
  const pointerModeRef = useRef<PointerMode>("idle");
  const pocketAnimationsRef = useRef<PocketAnimation[]>([]);
  const prevPocketedIdsRef = useRef<Set<string>>(new Set());
  const ballSpinRef = useRef<Map<string, BallSpinState>>(new Map());
  const snapAnimRef = useRef<{ startedAt: number; power: number; fired: boolean } | null>(null);
  const canInteractRef = useRef(false);
  const shootBusyRef = useRef(false);
  const onShootRef = useRef(onShoot);
  const onAimStateChangeRef = useRef(onAimStateChange);
  const displayedGroupsRef = useRef(displayedGroups);
  const pendingDisplayedGroupsRef = useRef<{ hostGroup: BallGroup | null; guestGroup: BallGroup | null } | null>(null);
  const lastAimEmitAtRef = useRef(0);
  const lastAimPayloadKeyRef = useRef<string>("");

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

  // Shot animation trigger
  useEffect(() => {
    if (animating) return;
    if (!game.lastShot || !game.lastShot.frames.length) return;
    if (game.lastShot.seq <= lastAnimatedSeqRef.current) return;
    playbackSettlingRef.current = false;
    playbackRef.current = {
      seq: game.lastShot.seq,
      frames: game.lastShot.frames,
      startedAt: performance.now(),
      baseBalls: game.balls,
      finalBalls: game.balls,
    };
    setAnimating(true);
    setAnimatingSeq(game.lastShot.seq);
    // Schedule realistic collision sounds based on shot frames
    const frameCount = game.lastShot.frames.length;
    // Initial ball hit
    window.setTimeout(() => SFX.ballHit(), 80);
    // Cushion bounces during longer shots
    if (frameCount > 60) {
      window.setTimeout(() => SFX.cushion(), 300);
    }
    if (frameCount > 150) {
      window.setTimeout(() => SFX.cushion(), 600);
      window.setTimeout(() => SFX.ballHit(), 450);
    }
  }, [animating, game]);

  useEffect(() => () => {
    if (drawLoopRef.current !== null) window.cancelAnimationFrame(drawLoopRef.current);
    if (powerReturnAnimRef.current !== null) window.cancelAnimationFrame(powerReturnAnimRef.current);
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
  const canInteract = Boolean(cueBall && isMyTurn && !animating && !shootBusy && game.status !== "finished");
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
  useEffect(() => { pointerModeRef.current = pointerMode; }, [pointerMode]);

  const turnControlVisible = Boolean(cueBall && game.status !== "finished" && !animating);
  const powerBarInteractive = canInteract && !isBallInHand;
  const powerVisual = clamp((power - POWER_MIN) / (1 - POWER_MIN), 0, 1);
  const remotePowerVisual = !isMyTurn
    ? opponentAim?.mode === "power"
      ? 0.62
      : opponentAim?.mode === "aim"
        ? 0.2
        : 0.08
    : 0;
  const displayedPowerVisual = isMyTurn
    ? isBallInHand
      ? 0.08
      : powerVisual
    : remotePowerVisual;

  const emitAimState = (next: { visible: boolean; angle: number; cueX?: number | null; cueY?: number | null; mode: AimPointerMode }, force = false) => {
    const handler = onAimStateChangeRef.current;
    if (!handler) return;
    const payload = {
      visible: next.visible,
      angle: Number.isFinite(next.angle) ? next.angle : 0,
      cueX: next.cueX ?? null,
      cueY: next.cueY ?? null,
      mode: next.mode,
    };
    const key = `${payload.visible ? 1 : 0}:${payload.mode}:${payload.angle.toFixed(3)}:${payload.cueX === null ? "n" : payload.cueX.toFixed(1)}:${payload.cueY === null ? "n" : payload.cueY.toFixed(1)}`;
    const now = performance.now();
    if (!force && key === lastAimPayloadKeyRef.current && now - lastAimEmitAtRef.current < AIM_SYNC_INTERVAL_MS) return;
    if (!force && payload.visible && now - lastAimEmitAtRef.current < AIM_SYNC_INTERVAL_MS) return;
    lastAimPayloadKeyRef.current = key;
    lastAimEmitAtRef.current = now;
    handler(payload);
  };

  useEffect(() => {
    if (!canInteract || animating || shootBusy || game.status === "finished" || pointerMode === "place") {
      emitAimState({ visible: false, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "idle" }, true);
    }
  }, [animating, canInteract, cueBall?.x, cueBall?.y, game.status, pointerMode, shootBusy]);

  const setPointerModeSafe = (next: PointerMode) => { pointerModeRef.current = next; setPointerMode(next); };

  const mapPowerFromDrag = (dragPx: number, fullTravelPx: number) => {
    const effective = Math.max(0, dragPx - POWER_DEADZONE_PX);
    const normalized = clamp(effective / Math.max(1, fullTravelPx), 0, 1);
    const curved = Math.pow(normalized, POWER_CURVE_EXPONENT);
    return clamp(POWER_MIN + curved * (1 - POWER_MIN), POWER_MIN, 1);
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
    emitAimState({ visible: false, angle: aimAngleRef.current, cueX: next.x, cueY: next.y, mode: "place" });
  };

  const updatePowerFromClientY = (clientY: number) => {
    const gesture = powerGestureRef.current;
    if (!gesture) return;
    const dragPx = Math.max(0, clientY - gesture.startY);
    const next = mapPowerFromDrag(dragPx, gesture.fullTravelPx);
    powerRef.current = next;
    setPower(next);
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "power" });
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
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
    emitAimState({ visible: false, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "idle" }, true);
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
    snapAnimRef.current = { startedAt: performance.now(), power: shotPower, fired: true };
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
    };

    if (state.isBallInHand) {
      localCuePlacementRef.current = { x: liveCueBall.x, y: liveCueBall.y };
    }

    // Fire the real shot immediately, then let visual/audio reactions happen in parallel.
    void onShootRef.current(payload).catch(() => {});
    animatePowerReturn(shotPower);
    window.requestAnimationFrame(() => { SFX.cueHit(payload.power); });
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canInteract || !powerRailRef.current) return;
    SFX.prime();
    if (powerReturnAnimRef.current !== null) {
      window.cancelAnimationFrame(powerReturnAnimRef.current);
      powerReturnAnimRef.current = null;
    }
    const rect = powerRailRef.current.getBoundingClientRect();
    powerGestureRef.current = {
      startY: event.clientY,
      fullTravelPx: Math.max(rect.height * POWER_FULL_TRAVEL_RATIO, 180),
    };
    powerReleaseGuardRef.current = false;
    powerRef.current = POWER_MIN;
    setPower(POWER_MIN);
    setPointerModeSafe("power");
    event.currentTarget.setPointerCapture?.(event.pointerId);
    emitAimState({ visible: true, angle: aimAngleRef.current, cueX: cueBall?.x ?? null, cueY: cueBall?.y ?? null, mode: "power" }, true);
  };

  const handlePowerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerModeRef.current !== "power") return;
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    commitPowerShot();
  };

  const handlePowerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    if (pointerModeRef.current === "power" && !powerReleaseGuardRef.current) commitPowerShot();
  };

  const handlePowerLostCapture = () => {
    if (pointerModeRef.current === "power") commitPowerShot();
  };

  useEffect(() => {
    if (pointerMode !== "power") return;
    const handleWindowUp = () => { if (pointerModeRef.current === "power") commitPowerShot(); };
    const handleWindowCancel = () => { if (pointerModeRef.current === "power") commitPowerShot(); };
    window.addEventListener("pointerup", handleWindowUp);
    window.addEventListener("pointercancel", handleWindowCancel);
    return () => { window.removeEventListener("pointerup", handleWindowUp); window.removeEventListener("pointercancel", handleWindowCancel); };
  }, [pointerMode, room.roomId, currentUserId, shootBusy, animating, canInteract, isBallInHand, needEightCall, selectedPocket]);

  const statusText = game.status === "finished"
    ? game.winnerUserId === currentUserId ? "Você venceu" : "Você perdeu"
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
        const nominalDuration = Math.max(frameStepMs, playback.frames.length * frameStepMs);
        const playbackDuration = Math.min(MAX_PLAYBACK_DURATION_MS, nominalDuration);
        const progress = clamp(elapsed / playbackDuration, 0, 1);
        const rawIndex = progress * Math.max(0, playback.frames.length - 1);
        const frameIndex = Math.min(playback.frames.length - 1, Math.floor(rawIndex));

        if (progress >= 1 || frameIndex >= playback.frames.length - 1) {
          drawBalls = frameToDisplayBalls(playback.frames[playback.frames.length - 1].balls, playback.baseBalls);
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
          if (!playbackSettlingRef.current) {
            playbackSettlingRef.current = true;
            window.setTimeout(() => {
              if (playbackRef.current?.seq !== playback.seq) return;
              lastAnimatedSeqRef.current = playback.seq;
              playbackRef.current = null;
              setDisplayBalls(playback.finalBalls);
              setAnimating(false);
              setAnimatingSeq(0);
            }, 0);
          }
        } else {
          const frameA = playback.frames[frameIndex];
          const frameB = playback.frames[Math.min(playback.frames.length - 1, frameIndex + 1)];
          const localT = clamp(rawIndex - frameIndex, 0, 1);
          drawBalls = interpolateFrameBalls(playback.baseBalls, frameA.balls, frameB.balls, localT);
          drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
        }
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

      const remoteVisible = Boolean(
        !animating
        && !state.canInteract
        && drawCueBall
        && game.turnUserId !== currentUserId
        && game.status !== "finished"
      );
      const remoteCueSource = remoteVisible ? drawCueBall : null;
      const remoteAimAngle = remoteCueSource
        ? (opponentAim && opponentAim.userId === game.turnUserId ? opponentAim.angle : estimateCueAngle(remoteCueSource, drawBalls))
        : 0;
      const remoteCueBall = remoteCueSource
        ? {
            id: remoteCueSource.id,
            number: 0,
            x: opponentAim && opponentAim.userId === game.turnUserId && opponentAim.cueX !== null ? opponentAim.cueX : remoteCueSource.x,
            y: opponentAim && opponentAim.userId === game.turnUserId && opponentAim.cueY !== null ? opponentAim.cueY : remoteCueSource.y,
            pocketed: false,
          }
        : null;
      if (remoteCueBall && game.ballInHandUserId === game.turnUserId) {
        drawBalls = drawBalls.map((ball) => ball.number === 0 ? { ...ball, x: remoteCueBall.x, y: remoteCueBall.y, pocketed: false } : ball);
        drawCueBall = drawBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? remoteCueBall;
      }

      updateBallSpinCache(ballSpinRef.current, drawBalls, now);
      const preview = drawCueBall && !animating && !(state.isBallInHand && state.pointerMode === "place") ? computeAimPreview(drawCueBall, drawBalls, drawAimAngleRef.current) : null;
      const remotePreview = remoteVisible && remoteCueBall && !(opponentAim && opponentAim.mode === "place" && game.ballInHandUserId === game.turnUserId)
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

      const remotePullRatio = opponentAim?.mode === "power" ? 0.32 : opponentAim?.mode === "aim" ? 0.08 : 0.05;

      drawPoolTable(
        context,
        tableCacheRef.current,
        cueSprite,
        drawBalls,
        drawCueBall,
        drawAimAngleRef.current,
        Boolean(drawCueBall && (state.canInteract || state.shootBusy || snapAnimRef.current) && !(state.isBallInHand && state.pointerMode === "place")),
        pullRatio,
        preview,
        state.needEightCall,
        state.selectedPocket,
        state.isBallInHand,
        pocketAnimationsRef.current,
        now,
        ballSpinRef.current,
        remoteVisible && remoteCueBall ? { cueBall: remoteCueBall, aimAngle: remoteAimAngle, preview: remotePreview, pullRatio: remotePullRatio } : null,
      );

      drawLoopRef.current = window.requestAnimationFrame(draw);
    };

    drawLoopRef.current = window.requestAnimationFrame(draw);
    return () => { if (drawLoopRef.current !== null) { window.cancelAnimationFrame(drawLoopRef.current); drawLoopRef.current = null; } };
  }, [animating, assetsVersion, cueSprite, game.ballInHandUserId, game.status, game.turnUserId, opponentAim]);

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
          className={`pool-stage__power ${powerBarInteractive ? "pool-stage__power--active" : ""} ${pointerMode === "power" ? "pool-stage__power--dragging" : ""} ${turnControlVisible ? "" : "pool-stage__power--hidden"} ${!isMyTurn && turnControlVisible ? "pool-stage__power--ghost" : ""} ${isMyTurn && !powerBarInteractive && turnControlVisible ? "pool-stage__power--standby" : ""}`}
          aria-hidden={!turnControlVisible}
          onPointerDown={handlePowerDown}
          onPointerMove={handlePowerMove}
          onPointerUp={handlePowerUp}
          onPointerCancel={handlePowerCancel}
          onLostPointerCapture={handlePowerLostCapture}
        >
          <div className="pool-stage__power-track">
            <div className="pool-stage__power-fill" style={{ height: `${Math.round(displayedPowerVisual * 100)}%`, top: "2px", bottom: "auto" }} />
            <div className="pool-stage__power-marker" style={{ top: `calc(${Math.round(displayedPowerVisual * 100)}% + 1px)` }} />
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
