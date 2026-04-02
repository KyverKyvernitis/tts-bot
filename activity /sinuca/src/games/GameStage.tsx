import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 12;
const BALL_DIAMETER = BALL_RADIUS * 2;
const PLAY_MIN_X = 54 + BALL_RADIUS;
const PLAY_MAX_X = TABLE_WIDTH - 54 - BALL_RADIUS;
const PLAY_MIN_Y = 44 + BALL_RADIUS;
const PLAY_MAX_Y = TABLE_HEIGHT - 44 - BALL_RADIUS;
const HEAD_STRING_X = TABLE_WIDTH * 0.29;
const BREAK_MAX_X = HEAD_STRING_X - BALL_RADIUS - 6;
const DEFAULT_CUE_X = TABLE_WIDTH * 0.23;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const RACK_APEX_X = 874;
const RACK_APEX_Y = TABLE_HEIGHT / 2;
const RACK_ROW_STEP_X = BALL_DIAMETER * 0.88;
const RACK_SPACING = BALL_DIAMETER * 1.02;
const MAX_PULL_DISTANCE = 150;
const POCKETS = [
  { id: 1, x: 48, y: 40 },
  { id: 2, x: TABLE_WIDTH / 2, y: 26 },
  { id: 3, x: TABLE_WIDTH - 48, y: 40 },
  { id: 4, x: 48, y: TABLE_HEIGHT - 40 },
  { id: 5, x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - 26 },
  { id: 6, x: TABLE_WIDTH - 48, y: TABLE_HEIGHT - 40 },
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
    1: "#f4d347",
    2: "#3966ea",
    3: "#d54143",
    4: "#7a4ad7",
    5: "#f18f34",
    6: "#31a15b",
    7: "#7b3025",
    8: "#17191d",
    9: "#f4d347",
    10: "#3966ea",
    11: "#d54143",
    12: "#7a4ad7",
    13: "#f18f34",
    14: "#31a15b",
    15: "#7b3025",
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

function drawBall(ctx: CanvasRenderingContext2D, ball: GameBallSnapshot) {
  const x = ball.x;
  const y = ball.y;
  const color = ballColor(ball.number);

  ctx.save();
  ctx.translate(x, y);
  ctx.shadowColor = "rgba(0, 0, 0, 0.30)";
  ctx.shadowBlur = 10;
  ctx.shadowOffsetY = 4;

  const baseGradient = ctx.createRadialGradient(-4, -4, 2, 0, 0, BALL_RADIUS + 6);
  if (ball.number === 0) {
    baseGradient.addColorStop(0, "#ffffff");
    baseGradient.addColorStop(1, "#d9e5ef");
  } else {
    baseGradient.addColorStop(0, ball.number === 8 ? "#4f5560" : "#fff4d0");
    baseGradient.addColorStop(0.24, ball.number === 8 ? "#232730" : color);
    baseGradient.addColorStop(1, ball.number === 8 ? "#090b10" : color);
  }

  ctx.beginPath();
  ctx.arc(0, 0, BALL_RADIUS, 0, Math.PI * 2);
  ctx.fillStyle = baseGradient;
  ctx.fill();

  if (ball.number >= 9) {
    ctx.beginPath();
    ctx.arc(0, 0, BALL_RADIUS - 1.2, 0, Math.PI * 2);
    ctx.fillStyle = "#fbfdff";
    ctx.fill();

    ctx.beginPath();
    ctx.roundRect(-BALL_RADIUS + 1, -5.4, BALL_RADIUS * 2 - 2, 10.8, 5.4);
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

function drawPoolTable(
  ctx: CanvasRenderingContext2D,
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

  const tableGradient = ctx.createLinearGradient(0, 0, 0, TABLE_HEIGHT);
  tableGradient.addColorStop(0, "#b02f1f");
  tableGradient.addColorStop(0.5, "#852414");
  tableGradient.addColorStop(1, "#a92f1d");
  ctx.fillStyle = tableGradient;
  ctx.beginPath();
  ctx.roundRect(8, 8, TABLE_WIDTH - 16, TABLE_HEIGHT - 16, 40);
  ctx.fill();

  const woodInner = ctx.createLinearGradient(0, 0, 0, TABLE_HEIGHT);
  woodInner.addColorStop(0, "rgba(255, 195, 160, 0.18)");
  woodInner.addColorStop(1, "rgba(0, 0, 0, 0.22)");
  ctx.fillStyle = woodInner;
  ctx.beginPath();
  ctx.roundRect(18, 18, TABLE_WIDTH - 36, TABLE_HEIGHT - 36, 32);
  ctx.fill();

  const feltInsetX = 54;
  const feltInsetY = 44;
  const feltWidth = TABLE_WIDTH - feltInsetX * 2;
  const feltHeight = TABLE_HEIGHT - feltInsetY * 2;

  const feltGradient = ctx.createLinearGradient(feltInsetX, feltInsetY, feltInsetX, feltInsetY + feltHeight);
  feltGradient.addColorStop(0, "#8de0ff");
  feltGradient.addColorStop(0.5, "#67c6eb");
  feltGradient.addColorStop(1, "#58b7dd");
  ctx.fillStyle = feltGradient;
  ctx.beginPath();
  ctx.roundRect(feltInsetX, feltInsetY, feltWidth, feltHeight, 28);
  ctx.fill();

  const glow = ctx.createRadialGradient(TABLE_WIDTH / 2, TABLE_HEIGHT / 2, 120, TABLE_WIDTH / 2, TABLE_HEIGHT / 2, TABLE_WIDTH * 0.45);
  glow.addColorStop(0, "rgba(255,255,255,0.20)");
  glow.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.roundRect(feltInsetX, feltInsetY, feltWidth, feltHeight, 28);
  ctx.fill();

  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(HEAD_STRING_X, feltInsetY + 14);
  ctx.lineTo(HEAD_STRING_X, TABLE_HEIGHT - feltInsetY - 14);
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(HEAD_STRING_X, TABLE_HEIGHT / 2, 26, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255,255,255,0.13)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  const cushionColor = "#a8d8ee";
  ctx.fillStyle = cushionColor;
  ctx.beginPath();
  ctx.moveTo(92, 52);
  ctx.lineTo(TABLE_WIDTH / 2 - 50, 52);
  ctx.lineTo(TABLE_WIDTH / 2 - 70, 70);
  ctx.lineTo(112, 70);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(TABLE_WIDTH / 2 + 50, 52);
  ctx.lineTo(TABLE_WIDTH - 92, 52);
  ctx.lineTo(TABLE_WIDTH - 112, 70);
  ctx.lineTo(TABLE_WIDTH / 2 + 70, 70);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(92, TABLE_HEIGHT - 52);
  ctx.lineTo(TABLE_WIDTH / 2 - 50, TABLE_HEIGHT - 52);
  ctx.lineTo(TABLE_WIDTH / 2 - 70, TABLE_HEIGHT - 70);
  ctx.lineTo(112, TABLE_HEIGHT - 70);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(TABLE_WIDTH / 2 + 50, TABLE_HEIGHT - 52);
  ctx.lineTo(TABLE_WIDTH - 92, TABLE_HEIGHT - 52);
  ctx.lineTo(TABLE_WIDTH - 112, TABLE_HEIGHT - 70);
  ctx.lineTo(TABLE_WIDTH / 2 + 70, TABLE_HEIGHT - 70);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(54, 92);
  ctx.lineTo(54, TABLE_HEIGHT - 92);
  ctx.lineTo(72, TABLE_HEIGHT - 112);
  ctx.lineTo(72, 112);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(TABLE_WIDTH - 54, 92);
  ctx.lineTo(TABLE_WIDTH - 54, TABLE_HEIGHT - 92);
  ctx.lineTo(TABLE_WIDTH - 72, TABLE_HEIGHT - 112);
  ctx.lineTo(TABLE_WIDTH - 72, 112);
  ctx.closePath();
  ctx.fill();

  for (const pocket of POCKETS) {
    const rim = ctx.createRadialGradient(pocket.x - 6, pocket.y - 6, 6, pocket.x, pocket.y, 34);
    rim.addColorStop(0, "rgba(81, 121, 143, 0.55)");
    rim.addColorStop(1, "rgba(8, 10, 14, 0.98)");
    ctx.beginPath();
    ctx.arc(pocket.x, pocket.y, 31, 0, Math.PI * 2);
    ctx.fillStyle = rim;
    ctx.fill();

    if (needEightCall && selectedPocket === pocket.id) {
      ctx.beginPath();
      ctx.arc(pocket.x, pocket.y, 18, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255, 246, 175, 0.95)";
      ctx.lineWidth = 3;
      ctx.stroke();
    }
  }

  if (cueBall && showGuide && preview) {
    const dirX = Math.cos(aimAngle);
    const dirY = Math.sin(aimAngle);

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.82)";
    ctx.lineWidth = 2.2;
    ctx.setLineDash([10, 8]);
    ctx.beginPath();
    ctx.moveTo(cueBall.x, cueBall.y);
    ctx.lineTo(preview.endX, preview.endY);
    ctx.stroke();
    ctx.restore();

    if (preview.contactX !== null && preview.contactY !== null) {
      ctx.beginPath();
      ctx.arc(preview.contactX, preview.contactY, BALL_RADIUS * 0.82, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,255,255,0.92)";
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    const cueGap = BALL_RADIUS + 10 + pullRatio * 74;
    const cueLength = 560;
    const tipX = cueBall.x - dirX * cueGap;
    const tipY = cueBall.y - dirY * cueGap;
    const buttX = cueBall.x - dirX * (cueGap + cueLength);
    const buttY = cueBall.y - dirY * (cueGap + cueLength);

    ctx.save();
    ctx.strokeStyle = "#d9ad73";
    ctx.lineWidth = 8;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(buttX, buttY);
    ctx.stroke();

    ctx.strokeStyle = "#f5e9d7";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(cueBall.x - dirX * (cueGap - 14), cueBall.y - dirY * (cueGap - 14));
    ctx.stroke();
    ctx.restore();
  }

  for (const ball of renderBalls) {
    if (ball.pocketed) continue;
    drawBall(ctx, ball);
  }

  if (cueBall && isBallInHand) {
    ctx.beginPath();
    ctx.arc(cueBall.x, cueBall.y, BALL_RADIUS + 10, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.38)";
    ctx.lineWidth = 2;
    ctx.setLineDash([8, 6]);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, onShoot, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [power, setPower] = useState(0.82);
  const [aimAngle, setAimAngle] = useState(0);
  const [pointerMode, setPointerMode] = useState<PointerMode>("idle");
  const [pullRatio, setPullRatio] = useState(0);
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState(0);
  const [selectedPocket, setSelectedPocket] = useState<number | null>(null);
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const powerRailRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef(0);
  const pointerMovedRef = useRef(false);
  const pullRatioRef = useRef(0);
  const aimAngleRef = useRef(0);
  const powerRef = useRef(power);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const isHost = currentUserId === room.hostUserId;
  const me = room.players.find((player) => player.userId === currentUserId) ?? (isHost ? host : guest);
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
    if (!animating) setDisplayBalls(game.balls);
  }, [animating, game.balls]);

  useEffect(() => {
    if (animating) return;
    if (!game.lastShot || !game.lastShot.frames.length) return;
    if (game.lastShot.seq <= lastAnimatedSeqRef.current) return;

    const frames = game.lastShot.frames;
    let frameIndex = 0;
    setAnimating(true);
    setAnimatingSeq(game.lastShot.seq);

    const tick = () => {
      const frame = frames[Math.min(frameIndex, frames.length - 1)];
      setDisplayBalls(frameToDisplayBalls(frame.balls, game.balls));
      frameIndex += 1;
      if (frameIndex < frames.length) {
        animationRef.current = window.setTimeout(tick, 24) as unknown as number;
        return;
      }
      lastAnimatedSeqRef.current = game.lastShot?.seq ?? 0;
      setDisplayBalls(game.balls);
      setAnimating(false);
      setAnimatingSeq(0);
      animationRef.current = null;
    };

    tick();
    return () => {
      if (animationRef.current !== null) {
        window.clearTimeout(animationRef.current);
        animationRef.current = null;
      }
    };
  }, [animating, game]);

  useEffect(() => () => {
    if (animationRef.current !== null) window.clearTimeout(animationRef.current);
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
  const cueLabel = game.tableType === "casual" ? "Amistoso" : `${game.stakeChips ?? 0}`;
  const myRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === myGroup).length;
  const opponentRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === opponentGroup).length;
  const leftRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === leftGroup).length;
  const rightRemaining = renderBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === rightGroup).length;
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;

  useEffect(() => {
    if (!cueBall) return;
    if (game.phase === "break") {
      setAimAngle(0);
      aimAngleRef.current = 0;
      return;
    }
    if (game.turnUserId !== currentUserId) {
      setAimAngle(Math.PI);
      aimAngleRef.current = Math.PI;
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

  const updatePullFromPoint = (point: LocalPoint) => {
    if (!cueBall) return;
    const distance = Math.hypot(point.x - cueBall.x, point.y - cueBall.y);
    const ratio = clamp((distance - 8) / MAX_PULL_DISTANCE, 0, 1);
    pullRatioRef.current = ratio;
    setPullRatio(ratio);
    pointerMovedRef.current = distance > 12;
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
    if (!cueBall || !canInteract) return;
    if (needEightCall && selectedPocket === null) return;
    const dragPower = pullRatioRef.current;
    if (dragPower < 0.1) {
      pullRatioRef.current = 0;
      setPullRatio(0);
      return;
    }
    const resolvedPower = clamp(0.14 + powerRef.current * dragPower * 0.86, 0.14, 1);
    await onShoot({
      angle: aimAngleRef.current,
      power: resolvedPower,
      cueX: isBallInHand ? cueBall.x : null,
      cueY: isBallInHand ? cueBall.y : null,
      calledPocket: needEightCall ? selectedPocket : null,
    });
    pullRatioRef.current = 0;
    setPullRatio(0);
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    pointerMovedRef.current = false;
    if (isBallInHand && pointInCircle(point, cueBall.x, cueBall.y, BALL_RADIUS * 2.25)) {
      setPointerMode("place");
      updateCuePositionFromPoint(point);
      return;
    }
    setPointerMode("aim");
    updateAimFromPoint(point);
    updatePullFromPoint(point);
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
      updatePullFromPoint(point);
    }
  };

  const handleTablePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const activeMode = pointerMode;
    setPointerMode("idle");
    if (activeMode === "aim" && pointerMovedRef.current && !shootBusy) {
      void releaseShot();
      return;
    }
    pullRatioRef.current = 0;
    setPullRatio(0);
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canInteract) return;
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
    if (pointerMode === "power") setPointerMode("idle");
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
    ? "Fim"
    : game.phase === "break"
      ? "Break"
      : game.phase === "open_table"
        ? "Mesa aberta"
        : game.phase === "eight_ball"
          ? "Bola 8"
          : isOpenTable
            ? "Pool"
            : myGroup === "solids"
              ? "Lisas"
              : "Listradas";

  const preview = useMemo(() => {
    if (!cueBall || animating) return null;
    return computeAimPreview(cueBall, renderBalls, aimAngle);
  }, [aimAngle, animating, cueBall, renderBalls]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = TABLE_WIDTH * dpr;
    canvas.height = TABLE_HEIGHT * dpr;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawPoolTable(
      context,
      renderBalls,
      cueBall,
      aimAngle,
      Boolean(cueBall && isMyTurn && !animating),
      pullRatio,
      preview,
      needEightCall && isMyTurn,
      selectedPocket,
      isBallInHand,
    );
  }, [aimAngle, animating, cueBall, isBallInHand, isMyTurn, needEightCall, preview, pullRatio, renderBalls, selectedPocket]);

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
          onPointerCancel={handlePowerUp}
        >
          <span className="pool-stage__power-cap" />
          <div className="pool-stage__power-track">
            <div className="pool-stage__power-fill" style={{ height: `${Math.round(power * 100)}%` }} />
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
