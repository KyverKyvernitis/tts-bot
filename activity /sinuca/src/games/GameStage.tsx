import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
import tableAsset from "../assets/game/pool-table-public.png";
import cueAsset from "../assets/game/pool-cue-public.png";
import powerFrameAsset from "../assets/game/power-meter-public.png";

const BALL_SPRITE_MODULES = import.meta.glob("../assets/game/balls/*.png", { eager: true, import: "default" }) as Record<string, string>;

const BALL_SPRITES_BY_NUMBER = new Map<number, string>();
Object.entries(BALL_SPRITE_MODULES).forEach(([path, src]) => {
  const match = path.match(/ball-(\d+)\.png$/);
  if (!match) return;
  BALL_SPRITES_BY_NUMBER.set(Number(match[1]), src);
});

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 13;
const BALL_DIAMETER = BALL_RADIUS * 2;
const BALL_DRAW_SIZE = 30;
const MAX_PLAYBACK_DURATION_MS = 2800;
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
const RACK_ROW_STEP_X = BALL_DIAMETER * 0.88;
const RACK_SPACING = BALL_DIAMETER * 1.02;
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

// Duração da animação da bola entrando na caçapa (ms)
const POCKET_ANIM_DURATION = 320;

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
  onShoot: (shot: ShotInput) => Promise<void>;
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
  // Deflection: where the cue ball goes after impact
  cueDeflectX: number | null;
  cueDeflectY: number | null;
};

// Animação de bola caindo na caçapa
type PocketAnimation = {
  ball: GameBallSnapshot;
  pocketX: number;
  pocketY: number;
  startedAt: number;
};

type SpriteBank = {
  table: HTMLImageElement;
  cue: HTMLImageElement;
  balls: Map<number, HTMLImageElement>;
};

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
};

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
    1: "#f1d54f",
    2: "#3761e2",
    3: "#d34b44",
    4: "#7948cb",
    5: "#f09236",
    6: "#2ca65a",
    7: "#6c2e21",
    8: "#14181f",
    9: "#f1d54f",
    10: "#3761e2",
    11: "#d34b44",
    12: "#7948cb",
    13: "#f09236",
    14: "#2ca65a",
    15: "#6c2e21",
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
      rackBalls.push({
        id: `ball-${number}`,
        number,
        x: rowX,
        y: startY + index * RACK_SPACING,
        pocketed: false,
      });
    });
  });

  return [cue, ...rackBalls];
}

function pointInCircle(point: LocalPoint, circleX: number, circleY: number, radius: number) {
  return Math.hypot(point.x - circleX, point.y - circleY) <= radius;
}

function findFirstBoundaryHit(cueBall: GameBallSnapshot, angle: number) {
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  const limits: number[] = [];

  if (dx > 0.0001) limits.push((PLAY_MAX_X - cueBall.x) / dx);
  if (dx < -0.0001) limits.push((PLAY_MIN_X - cueBall.x) / dx);
  if (dy > 0.0001) limits.push((PLAY_MAX_Y - cueBall.y) / dy);
  if (dy < -0.0001) limits.push((PLAY_MIN_Y - cueBall.y) / dy);

  const distance = Math.min(...limits.filter((value) => Number.isFinite(value) && value > 0));
  return {
    x: cueBall.x + dx * distance,
    y: cueBall.y + dy * distance,
    distance,
  };
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

  for (const ball of balls) {
    if (ball.pocketed || ball.number === 0) continue;
    const relX = ball.x - cueBall.x;
    const relY = ball.y - cueBall.y;
    const projection = relX * dx + relY * dy;
    if (projection <= BALL_DIAMETER) continue;
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

  // Calcular linha de deflexão da bola branca após impacto
  // Na física de sinuca (massas iguais, colisão elástica):
  // - Bola alvo: segue a linha dos centros (normal ao contato)
  // - Bola branca: deflecte perpendicularmente ao movimento da bola alvo
  if (hitBall && contactX !== null && contactY !== null) {
    // Vetor normal do impacto: do contact point até o centro da bola alvo
    const nx = hitBall.x - contactX;
    const ny = hitBall.y - contactY;
    const nlen = Math.hypot(nx, ny) || 1;
    const nnx = nx / nlen;
    const nny = ny / nlen;

    // Componente da velocidade ao longo do normal (vai para a bola alvo)
    const dotN = dx * nnx + dy * nny;
    // Componente residual (fica na bola branca)
    const remVx = dx - dotN * nnx;
    const remVy = dy - dotN * nny;
    const remLen = Math.hypot(remVx, remVy);

    const DEFLECT_DIST = 120;
    if (remLen > 0.05) {
      // A bola branca continua na direção perpendicular
      cueDeflectX = contactX + (remVx / remLen) * DEFLECT_DIST;
      cueDeflectY = contactY + (remVy / remLen) * DEFLECT_DIST;
    } else {
      // Tiro completamente frontal: bola branca para, mostramos apenas ponto
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
  };
}

// ─── Renderização das bolas ────────────────────────────────────────────────

function drawFallbackBall(ctx: CanvasRenderingContext2D, ball: GameBallSnapshot, scale = 1) {
  const x = ball.x;
  const y = ball.y;
  const r = BALL_RADIUS * scale;
  const color = ballColor(ball.number);

  ctx.save();
  ctx.translate(x, y);

  // Sombra
  ctx.shadowColor = "rgba(0, 0, 0, 0.35)";
  ctx.shadowBlur = 10 * scale;
  ctx.shadowOffsetY = 5 * scale;

  // Gradiente base 3D (iluminação de canto superior-esquerdo)
  const baseGradient = ctx.createRadialGradient(-r * 0.3, -r * 0.3, r * 0.05, 0, 0, r * 1.35);

  if (ball.number === 0) {
    // Bola branca
    baseGradient.addColorStop(0, "#ffffff");
    baseGradient.addColorStop(0.45, "#e8f0f8");
    baseGradient.addColorStop(1, "#b8cce0");
  } else if (ball.number === 8) {
    // Bola 8 preta
    baseGradient.addColorStop(0, "#4a5260");
    baseGradient.addColorStop(0.3, "#1e2330");
    baseGradient.addColorStop(1, "#050709");
  } else {
    baseGradient.addColorStop(0, "#fff8d8");
    baseGradient.addColorStop(0.18, color);
    baseGradient.addColorStop(1, shadeColor(color, -55));
  }

  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = baseGradient;
  ctx.fill();
  ctx.shadowColor = "transparent";

  // Bolas listradas (9-15): faixa branca + cor
  if (ball.number >= 9) {
    // Base branca com brilho
    ctx.beginPath();
    ctx.arc(0, 0, r - 0.8 * scale, 0, Math.PI * 2);
    ctx.fillStyle = "#f8faff";
    ctx.fill();

    // Faixa colorida larga no centro
    const stripeH = r * 1.04;
    ctx.save();
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.clip();
    const sg = ctx.createLinearGradient(-r, -stripeH, r, stripeH);
    sg.addColorStop(0, shadeColor(color, -30));
    sg.addColorStop(0.5, color);
    sg.addColorStop(1, shadeColor(color, -30));
    ctx.fillStyle = sg;
    ctx.fillRect(-r, -stripeH, r * 2, stripeH * 2);
    ctx.restore();
  }

  // Número da bola (disco branco central)
  if (ball.number > 0) {
    const diskR = r * 0.42;
    const diskGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, diskR);
    diskGrad.addColorStop(0, "#ffffff");
    diskGrad.addColorStop(1, "#e8eef4");
    ctx.beginPath();
    ctx.arc(0, 0, diskR, 0, Math.PI * 2);
    ctx.fillStyle = diskGrad;
    ctx.fill();

    const fontSize = clamp(Math.round((ball.number >= 10 ? 5.2 : 6.5) * scale), 4, 14);
    ctx.font = `700 ${fontSize}px Inter, system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = ball.number === 8 ? "#0a0c12" : "#1a1e2a";
    ctx.fillText(String(ball.number), 0, 0.5 * scale);
  }

  // Reflexo especular (brilho de luz no canto superior-esquerdo)
  const specGrad = ctx.createRadialGradient(-r * 0.34, -r * 0.38, 0, -r * 0.28, -r * 0.3, r * 0.58);
  specGrad.addColorStop(0, "rgba(255, 255, 255, 0.68)");
  specGrad.addColorStop(0.35, "rgba(255, 255, 255, 0.22)");
  specGrad.addColorStop(1, "rgba(255, 255, 255, 0)");
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = specGrad;
  ctx.fill();

  ctx.restore();
}

// Escurece/clareia uma cor hex
function shadeColor(hex: string, amount: number): string {
  const num = parseInt(hex.replace("#", ""), 16);
  const r = clamp((num >> 16) + amount, 0, 255);
  const g = clamp(((num >> 8) & 0xff) + amount, 0, 255);
  const b = clamp((num & 0xff) + amount, 0, 255);
  return `rgb(${r},${g},${b})`;
}

function drawBallSprite(ctx: CanvasRenderingContext2D, ball: GameBallSnapshot, sprite: HTMLImageElement | undefined, scale = 1) {
  if (!sprite || !sprite.complete || !sprite.naturalWidth) {
    drawFallbackBall(ctx, ball, scale);
    return;
  }
  const size = BALL_DRAW_SIZE * scale;
  // Sombra elíptica no chão
  ctx.save();
  ctx.fillStyle = "rgba(0, 0, 0, 0.28)";
  ctx.beginPath();
  ctx.ellipse(ball.x, ball.y + 9.2 * scale, 8.8 * scale, 3.8 * scale, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  ctx.drawImage(sprite, ball.x - size / 2, ball.y - size / 2, size, size);
}

// ─── Animação de bola entrando na caçapa ──────────────────────────────────

function drawPocketAnimation(
  ctx: CanvasRenderingContext2D,
  anim: PocketAnimation,
  now: number,
  sprite: HTMLImageElement | undefined,
) {
  const elapsed = now - anim.startedAt;
  const t = clamp(elapsed / POCKET_ANIM_DURATION, 0, 1);
  // ease-in: acelera ao cair na caçapa
  const eased = t * t;
  const scale = lerp(1, 0.08, eased);
  const alpha = lerp(1, 0, Math.pow(t, 0.7));
  if (alpha <= 0.02) return;

  ctx.save();
  ctx.globalAlpha = alpha;
  const ball = {
    ...anim.ball,
    x: lerp(anim.ball.x, anim.pocketX, eased),
    y: lerp(anim.ball.y, anim.pocketY, eased),
  };
  drawBallSprite(ctx, ball, sprite, scale);
  ctx.restore();
}

// ─── Guia de mira ─────────────────────────────────────────────────────────

function drawGuide(ctx: CanvasRenderingContext2D, cueBall: GameBallSnapshot, preview: AimPreview, aimAngle: number) {
  const hasHit = preview.contactX !== null && preview.contactY !== null && preview.hitBall;
  const lineEndX = hasHit ? preview.contactX! : preview.endX;
  const lineEndY = hasHit ? preview.contactY! : preview.endY;

  // Linha tracejada principal (trajetória da bola branca até o contato)
  ctx.save();
  ctx.strokeStyle = "rgba(208, 236, 255, 0.18)";
  ctx.lineWidth = 4.2;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.strokeStyle = "rgba(245, 250, 255, 0.95)";
  ctx.lineWidth = 1.15;
  ctx.lineCap = "round";
  ctx.setLineDash([6, 5]);
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();

  // Anel de mira ao redor da bola branca
  const ringX = cueBall.x - Math.cos(aimAngle) * (BALL_RADIUS * 0.56);
  const ringY = cueBall.y - Math.sin(aimAngle) * (BALL_RADIUS * 0.56);
  ctx.save();
  ctx.beginPath();
  ctx.arc(ringX, ringY, BALL_RADIUS * 1.06, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255,255,255,0.32)";
  ctx.lineWidth = 1.55;
  ctx.setLineDash([8, 7]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.arc(cueBall.x, cueBall.y, BALL_RADIUS * 1.82, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(173, 227, 255, 0.14)";
  ctx.lineWidth = 1.4;
  ctx.stroke();
  ctx.restore();

  if (hasHit) {
    const ghostX = preview.contactX!;
    const ghostY = preview.contactY!;
    const hitBall = preview.hitBall!;
    const dx = Math.cos(aimAngle);
    const dy = Math.sin(aimAngle);

    // Linha de trajetória da bola alvo (saindo do impacto)
    // Direção: linha dos centros (do ghost até o centro da bola alvo)
    const targetDX = hitBall.x - ghostX;
    const targetDY = hitBall.y - ghostY;
    const targetLen = Math.hypot(targetDX, targetDY) || 1;
    const tnx = targetDX / targetLen;
    const tny = targetDY / targetLen;
    const objectPathLen = 100;

    ctx.save();
    ctx.strokeStyle = "rgba(246, 250, 255, 0.85)";
    ctx.lineWidth = 1.55;
    ctx.lineCap = "round";
    ctx.setLineDash([8, 5]);
    ctx.beginPath();
    ctx.moveTo(hitBall.x, hitBall.y);
    ctx.lineTo(hitBall.x + tnx * objectPathLen, hitBall.y + tny * objectPathLen);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    // Bola fantasma no ponto de contato
    ctx.save();
    ctx.beginPath();
    ctx.arc(ghostX, ghostY, BALL_RADIUS * 0.72, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(250, 254, 255, 0.9)";
    ctx.lineWidth = 1.85;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(ghostX, ghostY, BALL_RADIUS * 1.02, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(166, 231, 255, 0.35)";
    ctx.lineWidth = 1.1;
    ctx.stroke();
    ctx.restore();

    // ── NOVO: Linha de deflexão da bola branca após o impacto ──────────────
    if (preview.cueDeflectX !== null && preview.cueDeflectY !== null) {
      const cdx = preview.cueDeflectX - ghostX;
      const cdy = preview.cueDeflectY - ghostY;
      const cdLen = Math.hypot(cdx, cdy);
      if (cdLen > 4) {
        ctx.save();
        // Linha de deflexão em azul-ciano suave
        ctx.strokeStyle = "rgba(120, 220, 255, 0.65)";
        ctx.lineWidth = 1.3;
        ctx.lineCap = "round";
        ctx.setLineDash([5, 6]);
        ctx.beginPath();
        ctx.moveTo(ghostX, ghostY);
        ctx.lineTo(preview.cueDeflectX, preview.cueDeflectY);
        ctx.stroke();
        ctx.setLineDash([]);
        // Ponto no final da deflexão
        ctx.beginPath();
        ctx.arc(preview.cueDeflectX, preview.cueDeflectY, 3, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(120, 220, 255, 0.55)";
        ctx.fill();
        ctx.restore();
      }
    }

    // Linha tangente na bola alvo (referência visual)
    const tangentX = Math.cos(aimAngle + Math.PI / 2) * BALL_RADIUS * 1.18;
    const tangentY = Math.sin(aimAngle + Math.PI / 2) * BALL_RADIUS * 1.18;
    ctx.save();
    ctx.strokeStyle = "rgba(246, 250, 255, 0.55)";
    ctx.lineWidth = 1.2;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(hitBall.x - tangentX, hitBall.y - tangentY);
    ctx.lineTo(hitBall.x + tangentX, hitBall.y + tangentY);
    ctx.stroke();
    ctx.restore();

    // Seta de direção na bola alvo
    const arrowLen = 18;
    const arrowAx = hitBall.x + tnx * arrowLen;
    const arrowAy = hitBall.y + tny * arrowLen;
    const perpX = -tny * 5;
    const perpY = tnx * 5;
    ctx.save();
    ctx.strokeStyle = "rgba(246, 250, 255, 0.7)";
    ctx.lineWidth = 1.2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(hitBall.x + tnx * 12, hitBall.y + tny * 12);
    ctx.lineTo(arrowAx, arrowAy);
    ctx.lineTo(arrowAx - tnx * 6 + perpX, arrowAy - tny * 6 + perpY);
    ctx.moveTo(arrowAx, arrowAy);
    ctx.lineTo(arrowAx - tnx * 6 - perpX, arrowAy - tny * 6 - perpY);
    ctx.stroke();
    ctx.restore();
  }
}

function drawCue(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  aimAngle: number,
  pullRatio: number,
  cueSprite: HTMLImageElement,
) {
  const dirX = Math.cos(aimAngle);
  const dirY = Math.sin(aimAngle);
  const cueGap = BALL_RADIUS + 4 + pullRatio * 118;
  const cueLength = 1028;
  const drawHeight = cueSprite.complete && cueSprite.naturalWidth ? Math.max(9, cueLength * (cueSprite.naturalHeight / cueSprite.naturalWidth)) : 10;

  ctx.save();
  ctx.translate(cueBall.x - dirX * cueGap, cueBall.y - dirY * cueGap);
  ctx.rotate(aimAngle);
  ctx.shadowColor = "rgba(0, 0, 0, 0.22)";
  ctx.shadowBlur = 8;
  ctx.shadowOffsetY = 1.4;
  if (cueSprite.complete && cueSprite.naturalWidth) {
    ctx.drawImage(cueSprite, -cueLength, -drawHeight / 2, cueLength, drawHeight);
  } else {
    ctx.strokeStyle = "#d9ad73";
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-cueLength, 0);
    ctx.stroke();
  }
  ctx.restore();
}

// ─── Render da mesa completa ───────────────────────────────────────────────

function drawPoolTable(
  ctx: CanvasRenderingContext2D,
  tableCache: HTMLCanvasElement | null,
  sprites: SpriteBank,
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
) {
  ctx.clearRect(0, 0, TABLE_WIDTH, TABLE_HEIGHT);

  if (tableCache) {
    ctx.drawImage(tableCache, 0, 0, TABLE_WIDTH, TABLE_HEIGHT);
  } else if (sprites.table.complete && sprites.table.naturalWidth) {
    ctx.drawImage(sprites.table, 0, 0, TABLE_WIDTH, TABLE_HEIGHT);
  }

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

  if (cueBall && showGuide && preview) {
    drawGuide(ctx, cueBall, preview, aimAngle);
    drawCue(ctx, cueBall, aimAngle, pullRatio, sprites.cue);
  }

  // Sombras das bolas (desenhadas antes)
  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    ctx.save();
    ctx.fillStyle = "rgba(0, 0, 0, 0.22)";
    ctx.beginPath();
    ctx.ellipse(ball.x + 1, ball.y + 8, 9, 3.8, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // Bolas normais
  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    const sprite = sprites.balls.get(ball.number);
    if (sprite && sprite.complete && sprite.naturalWidth) {
      const size = BALL_DRAW_SIZE;
      ctx.drawImage(sprite, ball.x - size / 2, ball.y - size / 2, size, size);
    } else {
      drawFallbackBall(ctx, ball, 1);
    }
  }

  // Animações de bolas entrando nas caçapas
  for (const anim of pocketAnimations) {
    drawPocketAnimation(ctx, anim, now, sprites.balls.get(anim.ball.number));
  }

  // Indicador de bola na mão
  if (cueBall && isBallInHand) {
    ctx.save();
    ctx.setLineDash([8, 6]);
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, BALL_RADIUS + 11, 0, Math.PI * 2);
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

function buildSpriteBank(): SpriteBank {
  const balls = new Map<number, HTMLImageElement>();
  Object.entries(BALL_SPRITE_MODULES).forEach(([path, src]) => {
    const match = path.match(/ball-(\d+)\.png$/);
    if (!match) return;
    balls.set(Number(match[1]), createImage(src));
  });

  return {
    table: createImage(tableAsset),
    cue: createImage(cueAsset),
    balls,
  };
}

// ─── Componente principal ──────────────────────────────────────────────────

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, onShoot, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [power, setPower] = useState(0.82);
  const [, setAimAngle] = useState(0);
  const [pointerMode, setPointerMode] = useState<PointerMode>("idle");
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState(0);
  const [selectedPocket, setSelectedPocket] = useState<number | null>(null);
  const [assetsVersion, setAssetsVersion] = useState(0);
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
  const aimAngleRef = useRef(0);
  const drawAimAngleRef = useRef(0);
  const powerRef = useRef(power);
  const powerReleaseGuardRef = useRef(false);
  const pointerModeRef = useRef<PointerMode>("idle");

  // Animações de bolas caindo nas caçapas
  const pocketAnimationsRef = useRef<PocketAnimation[]>([]);
  // Rastreia quais bolas já foram pocketadas (por id) para detectar novas
  const prevPocketedIdsRef = useRef<Set<string>>(new Set());

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
  const spriteBank = useMemo(() => buildSpriteBank(), []);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const isHost = currentUserId === room.hostUserId;
  const opponent = room.players.find((player) => player.userId !== currentUserId) ?? (isHost ? guest : host);
  const leftPlayer = host;
  const rightPlayer = guest;
  const leftGroup = game.hostGroup;
  const rightGroup = game.guestGroup;
  const myGroup = currentUserId === room.hostUserId ? game.hostGroup : game.guestGroup;
  const opponentGroup = currentUserId === room.hostUserId ? game.guestGroup : game.hostGroup;
  const isMyTurn = game.turnUserId === currentUserId;
  const isOpenTable = !game.hostGroup || !game.guestGroup;

  useEffect(() => {
    const notifyLoaded = () => setAssetsVersion((current) => current + 1);
    const unregister: Array<() => void> = [];
    const images = [spriteBank.table, spriteBank.cue, ...spriteBank.balls.values()];

    images.forEach((image) => {
      if (image.complete && image.naturalWidth) return;
      const handle = () => notifyLoaded();
      image.addEventListener("load", handle);
      image.addEventListener("error", handle);
      unregister.push(() => {
        image.removeEventListener("load", handle);
        image.removeEventListener("error", handle);
      });
    });

    return () => unregister.forEach((fn) => fn());
  }, [spriteBank]);

  useEffect(() => {
    tableCacheRef.current = makeTableCache(spriteBank.table);
  }, [assetsVersion, spriteBank]);

  useEffect(() => {
    if (!animating) setDisplayBalls(game.balls);
  }, [animating, game.balls]);

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
  }, [animating, game]);

  useEffect(() => () => {
    if (drawLoopRef.current !== null) window.cancelAnimationFrame(drawLoopRef.current);
  }, []);

  const renderBalls = useMemo(() => {
    const visibleNonCue = displayBalls.filter((ball) => !ball.pocketed && ball.number !== 0);
    if ((game.phase === "break" || game.shotSequence === 0) && visibleNonCue.length < 15) {
      return buildOpeningBalls(displayBalls);
    }
    return displayBalls;
  }, [displayBalls, game.phase, game.shotSequence]);

  const cueBall = renderBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const canInteract = Boolean(cueBall && isMyTurn && !animating && !shootBusy && game.status !== "finished");
  const isBallInHand = game.ballInHandUserId === currentUserId && canInteract;
  const cueLabel = game.tableType === "casual" ? "amistoso" : `${game.stakeChips ?? 0}`;
  const myRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === myGroup).length;
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;
  const leftPocketed = useMemo(() => pocketedNumbersForGroup(game.balls, leftGroup), [game.balls, leftGroup]);
  const rightPocketed = useMemo(() => pocketedNumbersForGroup(game.balls, rightGroup), [game.balls, rightGroup]);

  useEffect(() => {
    if (!cueBall) return;
    if (game.phase === "break") {
      setAimAngle(0);
      aimAngleRef.current = 0;
      drawAimAngleRef.current = 0;
      return;
    }
    if (game.turnUserId !== currentUserId) {
      setAimAngle(Math.PI);
      aimAngleRef.current = Math.PI;
      drawAimAngleRef.current = Math.PI;
    }
  }, [cueBall?.id, cueBall?.x, cueBall?.y, currentUserId, game.phase, game.turnUserId]);

  useEffect(() => {
    if (!needEightCall) setSelectedPocket(null);
  }, [needEightCall]);

  useEffect(() => {
    powerRef.current = power;
  }, [power]);

  useEffect(() => {
    pointerModeRef.current = pointerMode;
  }, [pointerMode]);

  const setPointerModeSafe = (next: PointerMode) => {
    pointerModeRef.current = next;
    setPointerMode(next);
  };

  const pointToLocal = (clientX: number, clientY: number) => {
    if (!tableWrapRef.current) return null;
    const rect = tableWrapRef.current.getBoundingClientRect();
    return {
      x: ((clientX - rect.left) / rect.width) * TABLE_WIDTH,
      y: ((clientY - rect.top) / rect.height) * TABLE_HEIGHT,
    };
  };

  const updateAimFromPoint = (point: LocalPoint) => {
    if (!cueBall) return;
    const angle = Math.atan2(cueBall.y - point.y, cueBall.x - point.x);
    aimAngleRef.current = angle;
  };

  const updateCuePositionFromPoint = (point: LocalPoint) => {
    if (!cueBall) return;
    const next = clampCuePosition(point.x, point.y, game.shotSequence === 0);
    setDisplayBalls((current) => current.map((ball) => (ball.number === 0 ? { ...ball, x: next.x, y: next.y, pocketed: false } : ball)));
  };

  const updatePowerFromClientY = (clientY: number) => {
    if (!powerRailRef.current) return;
    const rect = powerRailRef.current.getBoundingClientRect();
    const ratio = 1 - (clientY - rect.top) / rect.height;
    const next = clamp(ratio, 0.22, 1);
    powerRef.current = next;
    setPower(next);
  };

  const releaseShot = async () => {
    if (!cueBall || !canInteract || shootBusy) return;
    if (needEightCall && selectedPocket === null) return;
    const payload = {
      angle: aimAngleRef.current,
      power: clamp(powerRef.current, 0.22, 1),
      cueX: isBallInHand ? cueBall.x : null,
      cueY: isBallInHand ? cueBall.y : null,
      calledPocket: needEightCall ? selectedPocket : null,
    };
    console.log("[sinuca-frontend-shoot]", JSON.stringify({
      roomId: room.roomId,
      userId: currentUserId,
      isMyTurn,
      ...payload,
    }));
    await onShoot(payload);
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    pointerMovedRef.current = false;
    if (isBallInHand && pointInCircle(point, cueBall.x, cueBall.y, BALL_RADIUS * 2.2)) {
      setPointerModeSafe("place");
      updateCuePositionFromPoint(point);
      return;
    }
    setPointerModeSafe("aim");
    updateAimFromPoint(point);
    pointerMovedRef.current = true;
  };

  const handleTablePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    if (pointerMode === "place") {
      updateCuePositionFromPoint(point);
      return;
    }
    if (pointerMode === "aim") {
      updateAimFromPoint(point);
      pointerMovedRef.current = true;
    }
  };

  const handleTablePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    setPointerModeSafe("idle");
  };

  const commitPowerShot = () => {
    if (pointerModeRef.current !== "power" || powerReleaseGuardRef.current) return;
    powerReleaseGuardRef.current = true;
    setPointerModeSafe("idle");
    console.log("[sinuca-power-release]", JSON.stringify({ roomId: room.roomId, power: clamp(powerRef.current, 0.22, 1) }));
    void releaseShot();
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canInteract) return;
    powerReleaseGuardRef.current = false;
    setPointerModeSafe("power");
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updatePowerFromClientY(event.clientY);
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
    if (pointerModeRef.current === "power" && !powerReleaseGuardRef.current) {
      commitPowerShot();
    }
  };

  const handlePowerLostCapture = () => {
    if (pointerModeRef.current === "power") commitPowerShot();
  };

  useEffect(() => {
    if (pointerMode !== "power") return;
    const handleWindowUp = () => {
      if (pointerModeRef.current === "power") commitPowerShot();
    };
    const handleWindowCancel = () => {
      if (pointerModeRef.current === "power") commitPowerShot();
    };
    window.addEventListener("pointerup", handleWindowUp);
    window.addEventListener("pointercancel", handleWindowCancel);
    return () => {
      window.removeEventListener("pointerup", handleWindowUp);
      window.removeEventListener("pointercancel", handleWindowCancel);
    };
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
    : game.phase === "break"
      ? "break"
      : game.phase === "open_table"
        ? "mesa aberta"
        : game.phase === "eight_ball"
          ? "bola 8"
          : isOpenTable
            ? "pool"
            : myGroup === "solids"
              ? "lisas"
              : "listradas";

  useEffect(() => {
    renderStateRef.current = {
      renderBalls,
      cueBall,
      canInteract,
      pointerMode,
      power,
      needEightCall: needEightCall && isMyTurn,
      selectedPocket,
      isBallInHand,
      shootBusy,
    };
  }, [canInteract, cueBall, isBallInHand, isMyTurn, needEightCall, pointerMode, power, renderBalls, selectedPocket, shootBusy]);

  // ─── Loop de renderização ────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    const draw = () => {
      const now = performance.now();
      const dpr = Math.min(1.4, Math.max(1, window.devicePixelRatio || 1));
      const targetWidth = Math.round(TABLE_WIDTH * dpr);
      const targetHeight = Math.round(TABLE_HEIGHT * dpr);
      if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
      }
      context.setTransform(dpr, 0, 0, dpr, 0, 0);

      const state = renderStateRef.current;
      const targetAngle = aimAngleRef.current;
      drawAimAngleRef.current = lerpAngle(
        drawAimAngleRef.current,
        targetAngle,
        state.pointerMode === "aim" ? 0.84 : state.pointerMode === "power" ? 0.52 : 0.64,
      );

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

      // ── Detectar bolas recém-pocketadas e criar animações ──────────────
      const currentPocketedIds = new Set<string>();
      for (const ball of drawBalls) {
        if (ball.pocketed) currentPocketedIds.add(ball.id);
      }
      for (const ball of drawBalls) {
        if (ball.pocketed && !prevPocketedIdsRef.current.has(ball.id)) {
          // Esta bola acabou de ser pocketada!
          // Encontra a caçapa mais próxima para a animação
          let closestPocket = POCKETS[0];
          let minDist = Infinity;
          for (const pocket of POCKETS) {
            const d = Math.hypot(pocket.x - ball.x, pocket.y - ball.y);
            if (d < minDist) { minDist = d; closestPocket = pocket; }
          }
          pocketAnimationsRef.current.push({
            ball,
            pocketX: closestPocket.x,
            pocketY: closestPocket.y,
            startedAt: now,
          });
        }
      }
      prevPocketedIdsRef.current = currentPocketedIds;

      // Remover animações concluídas
      pocketAnimationsRef.current = pocketAnimationsRef.current.filter(
        (anim) => now - anim.startedAt < POCKET_ANIM_DURATION
      );

      const preview = drawCueBall && !animating
        ? computeAimPreview(drawCueBall, drawBalls, drawAimAngleRef.current)
        : null;

      drawPoolTable(
        context,
        tableCacheRef.current,
        spriteBank,
        drawBalls,
        drawCueBall,
        drawAimAngleRef.current,
        Boolean(drawCueBall && (state.canInteract || state.shootBusy)),
        state.pointerMode === "power"
          ? clamp(0.18 + state.power * 0.82, 0.18, 1)
          : state.pointerMode === "aim"
            ? 0.08
            : 0,
        preview,
        state.needEightCall,
        state.selectedPocket,
        state.isBallInHand,
        pocketAnimationsRef.current,
        now,
      );

      drawLoopRef.current = window.requestAnimationFrame(draw);
    };

    drawLoopRef.current = window.requestAnimationFrame(draw);
    return () => {
      if (drawLoopRef.current !== null) {
        window.cancelAnimationFrame(drawLoopRef.current);
        drawLoopRef.current = null;
      }
    };
  }, [animating, assetsVersion, spriteBank]);

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
                return (
                  <span key={`left-${index}`} className={`pool-stage__pip ${number !== null ? "pool-stage__pip--ball" : ""}`}>
                    {number !== null ? <img src={BALL_SPRITES_BY_NUMBER.get(number)} alt="" aria-hidden="true" /> : null}
                  </span>
                );
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
                return (
                  <span key={`right-${index}`} className={`pool-stage__pip ${number !== null ? "pool-stage__pip--ball" : ""}`}>
                    {number !== null ? <img src={BALL_SPRITES_BY_NUMBER.get(number)} alt="" aria-hidden="true" /> : null}
                  </span>
                );
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
          className={`pool-stage__power ${canInteract ? "pool-stage__power--active" : ""}`}
          onPointerDown={handlePowerDown}
          onPointerMove={handlePowerMove}
          onPointerUp={handlePowerUp}
          onPointerCancel={handlePowerCancel}
          onLostPointerCapture={handlePowerLostCapture}
        >
          <div className="pool-stage__power-track">
            <div className="pool-stage__power-fill" style={{ height: `${Math.round(power * 100)}%` }} />
            <img className="pool-stage__power-frame" src={powerFrameAsset} alt="" aria-hidden="true" />
            <div className="pool-stage__power-marker" style={{ bottom: `${Math.round(power * 100)}%` }} />
          </div>
          <small>{Math.round(power * 100)}</small>
        </div>

        <div className="pool-stage__table-shell">
          <div
            ref={tableWrapRef}
            className={`pool-stage__table-wrap ${canInteract ? "pool-stage__table-wrap--interactive" : ""}`}
            onPointerDown={handleTablePointerDown}
            onPointerMove={handleTablePointerMove}
            onPointerUp={handleTablePointerUp}
            onPointerCancel={handleTablePointerUp}
            onPointerLeave={(event) => {
              if (pointerMode === "idle") return;
              handleTablePointerUp(event);
            }}
          >
            <canvas ref={canvasRef} className="pool-stage__canvas" aria-hidden="true" />

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
