import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";
import tableAsset from "../assets/game/pool-table-mobile.svg";
import cueAsset from "../assets/game/pool-cue-mobile.svg";
import powerFrameAsset from "../assets/game/power-meter-frame.svg";

const BALL_SPRITE_MODULES = import.meta.glob("../assets/game/balls/*.svg", { eager: true, import: "default" }) as Record<string, string>;

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 13;
const BALL_DIAMETER = BALL_RADIUS * 2;
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
};

type SpriteBank = {
  table: HTMLImageElement;
  cue: HTMLImageElement;
  balls: Map<number, HTMLImageElement>;
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

  return {
    endX: cueBall.x + dx * hitDistance,
    endY: cueBall.y + dy * hitDistance,
    hitBall,
    contactX,
    contactY,
  };
}

function drawFallbackBall(ctx: CanvasRenderingContext2D, ball: GameBallSnapshot) {
  const x = ball.x;
  const y = ball.y;
  const color = ballColor(ball.number);

  ctx.save();
  ctx.translate(x, y);
  ctx.shadowColor = "rgba(0, 0, 0, 0.26)";
  ctx.shadowBlur = 8;
  ctx.shadowOffsetY = 4;

  const baseGradient = ctx.createRadialGradient(-4, -4, 2, 0, 0, BALL_RADIUS + 6);
  if (ball.number === 0) {
    baseGradient.addColorStop(0, "#ffffff");
    baseGradient.addColorStop(1, "#d9e5ef");
  } else {
    baseGradient.addColorStop(0, ball.number === 8 ? "#515964" : "#fff4d0");
    baseGradient.addColorStop(0.24, ball.number === 8 ? "#232730" : color);
    baseGradient.addColorStop(1, ball.number === 8 ? "#090b10" : color);
  }

  ctx.beginPath();
  ctx.arc(0, 0, BALL_RADIUS, 0, Math.PI * 2);
  ctx.fillStyle = baseGradient;
  ctx.fill();

  if (ball.number >= 9) {
    ctx.beginPath();
    ctx.arc(0, 0, BALL_RADIUS - 1.1, 0, Math.PI * 2);
    ctx.fillStyle = "#fbfdff";
    ctx.fill();

    ctx.beginPath();
    ctx.roundRect(-BALL_RADIUS + 1, -5.2, BALL_RADIUS * 2 - 2, 10.4, 5.2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  if (ball.number > 0) {
    ctx.beginPath();
    ctx.arc(0, 0, 5.4, 0, Math.PI * 2);
    ctx.fillStyle = ball.number === 8 ? "#f4f6fa" : "#fdfdfd";
    ctx.fill();

    ctx.fillStyle = ball.number === 8 ? "#091018" : "#182230";
    ctx.font = "700 7px Inter, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(ball.number), 0, 0.5);
  }

  ctx.restore();
}

function drawBallSprite(ctx: CanvasRenderingContext2D, ball: GameBallSnapshot, sprite: HTMLImageElement | undefined) {
  if (!sprite || !sprite.complete || !sprite.naturalWidth) {
    drawFallbackBall(ctx, ball);
    return;
  }
  const size = 31;
  ctx.save();
  ctx.fillStyle = "rgba(0, 0, 0, 0.22)";
  ctx.beginPath();
  ctx.ellipse(ball.x, ball.y + 9.7, 9.2, 4.1, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  ctx.drawImage(sprite, ball.x - size / 2, ball.y - size / 2, size, size);
}

function drawGuide(ctx: CanvasRenderingContext2D, cueBall: GameBallSnapshot, preview: AimPreview, aimAngle: number) {
  ctx.save();
  ctx.strokeStyle = "rgba(164, 228, 255, 0.18)";
  ctx.lineWidth = 8.6;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(preview.endX, preview.endY);
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.strokeStyle = "rgba(247, 251, 255, 0.98)";
  ctx.lineWidth = 1.9;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cueBall.x, cueBall.y);
  ctx.lineTo(preview.endX, preview.endY);
  ctx.stroke();
  ctx.restore();

  const ringX = cueBall.x - Math.cos(aimAngle) * (BALL_RADIUS * 0.45);
  const ringY = cueBall.y - Math.sin(aimAngle) * (BALL_RADIUS * 0.45);
  ctx.save();
  ctx.beginPath();
  ctx.arc(ringX, ringY, BALL_RADIUS * 0.92, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255,255,255,0.38)";
  ctx.lineWidth = 1.8;
  ctx.setLineDash([8, 7]);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(cueBall.x, cueBall.y, BALL_RADIUS * 1.68, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(176, 235, 255, 0.18)";
  ctx.lineWidth = 2;
  ctx.setLineDash([]);
  ctx.stroke();
  ctx.restore();

  if (preview.contactX !== null && preview.contactY !== null) {
    ctx.save();
    ctx.beginPath();
    ctx.arc(preview.contactX, preview.contactY, BALL_RADIUS * 0.72, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(252,255,255,0.98)";
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(preview.contactX, preview.contactY, BALL_RADIUS * 0.34, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(190, 244, 255, 0.7)";
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.restore();
  }

  if (preview.hitBall) {
    const normalX = Math.cos(aimAngle + Math.PI / 2) * BALL_RADIUS * 1.4;
    const normalY = Math.sin(aimAngle + Math.PI / 2) * BALL_RADIUS * 1.4;
    const tailX = Math.cos(aimAngle) * BALL_DIAMETER * 0.7;
    const tailY = Math.sin(aimAngle) * BALL_DIAMETER * 0.7;
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.92)";
    ctx.lineWidth = 1.8;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(preview.hitBall.x - normalX, preview.hitBall.y - normalY);
    ctx.lineTo(preview.hitBall.x + normalX, preview.hitBall.y + normalY);
    ctx.moveTo(preview.hitBall.x, preview.hitBall.y);
    ctx.lineTo(preview.hitBall.x + tailX, preview.hitBall.y + tailY);
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
  const cueGap = BALL_RADIUS + 8 + pullRatio * 88;
  const cueLength = 860;
  const drawHeight = 15;

  ctx.save();
  ctx.translate(cueBall.x - dirX * cueGap, cueBall.y - dirY * cueGap);
  ctx.rotate(aimAngle);
  ctx.shadowColor = "rgba(0, 0, 0, 0.28)";
  ctx.shadowBlur = 10;
  ctx.shadowOffsetY = 2;
  if (cueSprite.complete && cueSprite.naturalWidth) {
    ctx.drawImage(cueSprite, -cueLength, -drawHeight / 2, cueLength, drawHeight);
  } else {
    ctx.strokeStyle = "#d9ad73";
    ctx.lineWidth = 8;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-cueLength, 0);
    ctx.stroke();
  }
  ctx.restore();
}

function drawPoolTable(
  ctx: CanvasRenderingContext2D,
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
) {
  ctx.clearRect(0, 0, TABLE_WIDTH, TABLE_HEIGHT);

  if (sprites.table.complete && sprites.table.naturalWidth) {
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

  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    drawBallSprite(ctx, ball, sprites.balls.get(ball.number));
  }

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
    const match = path.match(/ball-(\d+)\.svg$/);
    if (!match) return;
    balls.set(Number(match[1]), createImage(src));
  });

  return {
    table: createImage(tableAsset),
    cue: createImage(cueAsset),
    balls,
  };
}

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, onShoot, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [power, setPower] = useState(0.82);
  const [aimAngle, setAimAngle] = useState(0);
  const [pointerMode, setPointerMode] = useState<PointerMode>("idle");
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState(0);
  const [selectedPocket, setSelectedPocket] = useState<number | null>(null);
  const [assetsVersion, setAssetsVersion] = useState(0);
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const powerRailRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const drawLoopRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef(0);
  const pointerMovedRef = useRef(false);
  const aimAngleRef = useRef(0);
  const drawAimAngleRef = useRef(0);
  const powerRef = useRef(power);
  const powerReleaseGuardRef = useRef(false);
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
    if (!animating) setDisplayBalls(game.balls);
  }, [animating, game.balls]);

  useEffect(() => {
    if (animating) return;
    if (!game.lastShot || !game.lastShot.frames.length) return;
    if (game.lastShot.seq <= lastAnimatedSeqRef.current) return;

    const frames = game.lastShot.frames;
    const frameStepMs = 1000 / 60;
    let startedAt = 0;
    setAnimating(true);
    setAnimatingSeq(game.lastShot.seq);

    const tick = (timestamp: number) => {
      if (!startedAt) startedAt = timestamp;
      const elapsed = timestamp - startedAt;
      const frameIndex = Math.min(frames.length - 1, Math.floor(elapsed / frameStepMs));
      const frame = frames[frameIndex];
      setDisplayBalls(frameToDisplayBalls(frame.balls, game.balls));
      if (frameIndex < frames.length - 1) {
        animationRef.current = window.requestAnimationFrame(tick);
        return;
      }
      lastAnimatedSeqRef.current = game.lastShot?.seq ?? 0;
      setDisplayBalls(game.balls);
      setAnimating(false);
      setAnimatingSeq(0);
      animationRef.current = null;
    };

    animationRef.current = window.requestAnimationFrame(tick);
    return () => {
      if (animationRef.current !== null) {
        window.cancelAnimationFrame(animationRef.current);
        animationRef.current = null;
      }
    };
  }, [animating, game]);

  useEffect(() => () => {
    if (animationRef.current !== null) window.cancelAnimationFrame(animationRef.current);
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
  const leftRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === leftGroup).length;
  const rightRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === rightGroup).length;
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;

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
    setAimAngle(angle);
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
      setPointerMode("place");
      updateCuePositionFromPoint(point);
      return;
    }
    setPointerMode("aim");
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
    setPointerMode("idle");
  };

  const commitPowerShot = () => {
    if (pointerMode !== "power" || powerReleaseGuardRef.current) return;
    powerReleaseGuardRef.current = true;
    setPointerMode("idle");
    console.log("[sinuca-power-release]", JSON.stringify({ roomId: room.roomId, power: clamp(powerRef.current, 0.22, 1) }));
    void releaseShot();
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canInteract) return;
    powerReleaseGuardRef.current = false;
    setPointerMode("power");
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerMode !== "power") return;
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    commitPowerShot();
  };

  const handlePowerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    if (pointerMode === "power" && !powerReleaseGuardRef.current) {
      powerReleaseGuardRef.current = true;
      setPointerMode("idle");
    }
  };

  const handlePowerLostCapture = () => {
    if (pointerMode === "power") commitPowerShot();
  };

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

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    const draw = () => {
      const dpr = Math.max(1, window.devicePixelRatio || 1);
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
        state.pointerMode === "aim" ? 0.34 : state.pointerMode === "power" ? 0.2 : 0.26,
      );
      const preview = state.cueBall && !animating
        ? computeAimPreview(state.cueBall, state.renderBalls, drawAimAngleRef.current)
        : null;

      drawPoolTable(
        context,
        spriteBank,
        state.renderBalls,
        state.cueBall,
        drawAimAngleRef.current,
        Boolean(state.cueBall && (state.canInteract || state.shootBusy)),
        state.pointerMode === "power"
          ? clamp(0.18 + state.power * 0.82, 0.18, 1)
          : state.pointerMode === "aim"
            ? 0.08
            : 0,
        preview,
        state.needEightCall,
        state.selectedPocket,
        state.isBallInHand,
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
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`left-${index}`} className={`pool-stage__pip ${index < (leftGroup ? 7 - leftRemaining : 0) ? "pool-stage__pip--filled" : ""}`} />
              ))}
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
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`right-${index}`} className={`pool-stage__pip ${index < (rightGroup ? 7 - rightRemaining : 0) ? "pool-stage__pip--filled" : ""}`} />
              ))}
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
