import type { AimPointerMode, GameBallSnapshot } from "../types/activity";

export type AimPreview = {
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

export type AimRenderMetrics = {
  ballRadius: number;
  ballVisualRadius: number;
};

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function lerp(from: number, to: number, t: number) {
  return from + (to - from) * t;
}

export function drawAimLine(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  preview: AimPreview,
  illegalTarget: boolean,
  options?: { showTargetGuide?: boolean },
) {
  const hasHit = preview.contactX !== null && preview.contactY !== null && preview.hitBall;
  const lineEndX = hasHit ? preview.contactX! : preview.endX;
  const lineEndY = hasHit ? preview.contactY! : preview.endY;

  ctx.save();
  ctx.strokeStyle = "rgba(200, 230, 255, 0.10)";
  ctx.lineWidth = 6;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.strokeStyle = "rgba(245, 250, 255, 0.88)";
  ctx.lineWidth = 2.15;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();
  ctx.restore();

  const showTargetGuide = options?.showTargetGuide ?? true;

  if (showTargetGuide && !illegalTarget && hasHit && preview.hitBall && preview.targetGuideX !== null && preview.targetGuideY !== null) {
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
  }
}

export function drawIllegalAimMarker(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(x - 5.8, y - 5.8);
  ctx.lineTo(x + 5.8, y + 5.8);
  ctx.moveTo(x - 5.8, y + 5.8);
  ctx.lineTo(x + 5.8, y - 5.8);
  ctx.strokeStyle = "rgba(255, 88, 88, 0.98)";
  ctx.lineCap = "round";
  ctx.lineWidth = 2.8;
  ctx.stroke();
  ctx.restore();
}

export function drawGhostBall(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  preview: AimPreview,
  powerRatio: number,
  illegalTarget: boolean,
  metrics: AimRenderMetrics,
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
    const ghostRadius = metrics.ballVisualRadius * ghostScale;

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

export function drawCue(
  ctx: CanvasRenderingContext2D,
  cueBall: GameBallSnapshot,
  aimAngle: number,
  pullRatio: number,
  cueSprite: HTMLImageElement,
  metrics: AimRenderMetrics,
) {
  const dirX = Math.cos(aimAngle);
  const dirY = Math.sin(aimAngle);
  const cueGap = metrics.ballRadius + 14 + pullRatio * 72;
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

export function drawRemoteAimOverlay(
  ctx: CanvasRenderingContext2D,
  cueSprite: HTMLImageElement,
  cueBall: GameBallSnapshot,
  aimAngle: number,
  preview: AimPreview | null,
  pullRatio: number,
  mode: AimPointerMode,
  metrics: AimRenderMetrics,
  drawBallOverlay: (ctx: CanvasRenderingContext2D, ball: GameBallSnapshot, alpha: number) => void,
) {
  const lineEndX = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactX : preview?.endX ?? (cueBall.x + Math.cos(aimAngle) * 420);
  const lineEndY = preview && preview.contactX !== null && preview.contactY !== null ? preview.contactY : preview?.endY ?? (cueBall.y + Math.sin(aimAngle) * 420);
  const showPlacementRing = mode === "place";
  const showGuide = mode !== "place";

  if (showPlacementRing) {
    ctx.save();
    ctx.globalAlpha = 0.96;
    drawBallOverlay(ctx, cueBall, 1);
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.92;
    ctx.setLineDash([7, 5]);
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, metrics.ballVisualRadius + 8, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.restore();
  }

  if (showGuide) {
    if (preview) {
      ctx.save();
      ctx.globalAlpha = mode === "power" ? 0.84 : 0.76;
      drawAimLine(ctx, cueBall, preview, false, { showTargetGuide: false });
      ctx.restore();
    } else {
      ctx.save();
      ctx.globalAlpha = mode === "power" ? 0.9 : 0.84;
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

  if (showGuide) {
    ctx.save();
    ctx.globalAlpha = mode === "power" ? 0.88 : 0.8;
    drawCue(ctx, cueBall, aimAngle, pullRatio, cueSprite, metrics);
    ctx.restore();
  }
}
