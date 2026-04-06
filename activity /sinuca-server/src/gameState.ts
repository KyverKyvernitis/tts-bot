import type { BallGroup, GameBallSnapshot, GamePhase, GameShotFrame, GameShotSnapshot, GameSnapshot } from "./messages.js";
import type { RoomRecord } from "./rooms.js";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 13;
const BALL_DIAMETER = BALL_RADIUS * 2;
const POCKET_RADIUS = 32;
const POCKET_CAPTURE_RADIUS = POCKET_RADIUS + 4;
const POCKET_FUNNEL_RADIUS = POCKET_RADIUS + 20;
const POCKET_WALL_SKIP_RADIUS = POCKET_RADIUS + 18;
const RAIL_MARGIN_X = 69;
const RAIL_MARGIN_Y = 50;
const HEAD_STRING_X = 328;
const DEFAULT_CUE_X = 248;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const MIN_SHOT_SPEED = 0.014;
const MAX_SHOT_SPEED = 12.6;
const POWER_FLOOR = 0.00035;
const SOFT_STOP_SPEED = 0.0105;
const HARD_STOP_SPEED = 0.0026;
const MIN_SPEED = SOFT_STOP_SPEED;
const MAX_STEPS = 1800;
const FRAME_SAMPLE_EVERY = 2;
const MAX_SUBSTEPS = 20;
const BALL_BALL_RESTITUTION = 0.905;
const BALL_TANGENT_FRICTION = 0.058;
const BALL_SPIN_TRANSFER = 0.26;
const RAIL_RESTITUTION = 0.765;
const RAIL_TANGENT_FRICTION = 0.915;
const RAIL_SPIN_TO_TANGENT = 0.165;
const RAIL_SPIN_KEEP = 0.72;
const SLIDING_DRAG = 0.99435;
const ROLLING_DRAG = 0.99812;
const HIGH_SPEED_DRAG = 0.99725;
const ROLL_SYNC_RATE = 0.28;
const ROLL_KEEP = 0.9992;
const SPIN_DECAY = 0.9898;
const SPIN_CURVE_FACTOR = 0.00115;
const SPIN_STOP_SPEED = 0.016;
const ROLL_STOP_SPEED = 0.02;
const BACKSPIN_DRAG_FACTOR = 0.013;
const OVERSPIN_PUSH_FACTOR = 0.009;
const SHOT_SIDE_SPIN_GAIN = 0.38;
const SHOT_ROLL_SPIN_GAIN = 0.6;

const POCKETS = [
  { x: 54, y: 42 },
  { x: TABLE_WIDTH / 2, y: 28 },
  { x: TABLE_WIDTH - 54, y: 42 },
  { x: 54, y: TABLE_HEIGHT - 42 },
  { x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - 28 },
  { x: TABLE_WIDTH - 54, y: TABLE_HEIGHT - 42 },
] as const;

interface PhysicsBall extends GameBallSnapshot {
  vx: number;
  vy: number;
  roll: number;
  sideSpin: number;
}

interface ShotOutcome {
  frames: GameShotFrame[];
  pocketedNumbers: number[];
  cuePocketed: boolean;
  nextTurnUserId: string;
  firstHitNumber: number | null;
  foulReason: string | null;
  winnerUserId: string | null;
  eightPocket: boolean;
}

interface PocketEvent {
  number: number;
  pocketIndex: number;
}

interface GameRecord extends Omit<GameSnapshot, "balls" | "lastShot"> {
  balls: PhysicsBall[];
  lastShot: GameShotSnapshot | null;
}

const games = new Map<string, GameRecord>();

function makeGameId() {
  return `game-${Math.random().toString(36).slice(2, 10)}`;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function clampUnit(value: number) {
  return clamp(value, -1, 1);
}

function clothBias(ball: PhysicsBall) {
  if (ball.number === 0) return 1;
  const pattern = ((ball.number * 17) % 9) - 4;
  return 1 + pattern * 0.004;
}

function createBall(number: number, x: number, y: number): PhysicsBall {
  return {
    id: `ball-${number}`,
    number,
    x,
    y,
    pocketed: false,
    vx: 0,
    vy: 0,
    roll: 0,
    sideSpin: 0,
  };
}

function rackBalls(): PhysicsBall[] {
  const balls: PhysicsBall[] = [createBall(0, DEFAULT_CUE_X, DEFAULT_CUE_Y)];
  const apexX = 922;
  const apexY = TABLE_HEIGHT / 2;
  const rowStepX = BALL_DIAMETER * 0.866; // tight triangular packing
  const spacing = BALL_DIAMETER * 1.0; // touching, no gaps
  const rackRows = [
    [1],
    [9, 2],
    [10, 8, 11],
    [3, 14, 7, 12],
    [15, 6, 13, 4, 5],
  ];

  for (let row = 0; row < rackRows.length; row += 1) {
    const rowX = apexX + row * rowStepX;
    const rowBalls = rackRows[row];
    const startY = apexY - ((rowBalls.length - 1) * spacing) / 2;
    rowBalls.forEach((number, index) => {
      const jx = (Math.random() - 0.5) * 0.5;
      const jy = (Math.random() - 0.5) * 0.5;
      balls.push(createBall(number, rowX + jx, startY + index * spacing + jy));
    });
  }

  return balls;
}

function cloneBalls(balls: PhysicsBall[]): PhysicsBall[] {
  return balls.map((ball) => ({ ...ball }));
}

function toFrameBalls(balls: PhysicsBall[]) {
  return balls.map((ball) => ({
    id: ball.id,
    x: Number(ball.x.toFixed(2)),
    y: Number(ball.y.toFixed(2)),
    pocketed: ball.pocketed,
  }));
}

function currentFrame(balls: PhysicsBall[]): GameShotFrame {
  return { balls: toFrameBalls(balls) };
}

function framesNearlyEqual(a: GameShotFrame, b: GameShotFrame, tolerance = 0.08) {
  const bMap = new Map(b.balls.map((ball) => [ball.id, ball]));
  for (const ball of a.balls) {
    const other = bMap.get(ball.id);
    if (!other) continue;
    if (ball.pocketed !== other.pocketed) return false;
    if (Math.abs(ball.x - other.x) > tolerance || Math.abs(ball.y - other.y) > tolerance) return false;
  }
  return true;
}

function trimTrailingSettledFrames(frames: GameShotFrame[]) {
  if (frames.length <= 2) return frames;
  let keepUntil = frames.length - 1;
  const finalFrame = frames[frames.length - 1];
  while (keepUntil > 1 && framesNearlyEqual(frames[keepUntil - 1], finalFrame)) {
    keepUntil -= 1;
  }
  return frames.slice(0, keepUntil + 1);
}

function toSnapshot(game: GameRecord, sinceSeq?: number | null): GameSnapshot {
  return {
    ...game,
    balls: game.balls.map(({ vx: _vx, vy: _vy, roll: _roll, sideSpin: _sideSpin, ...ball }) => ball),
    lastShot: game.lastShot && (!sinceSeq || game.lastShot.seq > sinceSeq) ? game.lastShot : null,
  };
}

function ballIsMoving(ball: PhysicsBall) {
  if (ball.pocketed) return false;
  const speed = Math.hypot(ball.vx, ball.vy);
  return speed > SOFT_STOP_SPEED || Math.abs(ball.sideSpin) > SPIN_STOP_SPEED || Math.abs(ball.roll) > ROLL_STOP_SPEED;
}

function nearPocket(x: number, y: number) {
  const pocketSkipRadius = POCKET_WALL_SKIP_RADIUS;
  for (const pocket of POCKETS) {
    if (Math.hypot(x - pocket.x, y - pocket.y) < pocketSkipRadius) return true;
  }
  return false;
}

function rotateVelocity(ball: PhysicsBall, radians: number) {
  if (!Number.isFinite(radians) || Math.abs(radians) < 0.000001) return;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  const nextVx = ball.vx * cos - ball.vy * sin;
  const nextVy = ball.vx * sin + ball.vy * cos;
  ball.vx = nextVx;
  ball.vy = nextVy;
}

function distancePointToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number) {
  const abx = bx - ax;
  const aby = by - ay;
  const lengthSq = abx * abx + aby * aby;
  if (lengthSq <= 0.000001) return Math.hypot(px - ax, py - ay);
  const t = clamp(((px - ax) * abx + (py - ay) * aby) / lengthSq, 0, 1);
  const closestX = ax + abx * t;
  const closestY = ay + aby * t;
  return Math.hypot(px - closestX, py - closestY);
}

function applyRailResponse(ball: PhysicsBall, normalX: number, normalY: number) {
  const tangentX = -normalY;
  const tangentY = normalX;
  const normalVelocity = ball.vx * normalX + ball.vy * normalY;
  const tangentVelocity = ball.vx * tangentX + ball.vy * tangentY;
  const nextNormal = -normalVelocity * RAIL_RESTITUTION;
  const nextTangent = tangentVelocity * RAIL_TANGENT_FRICTION + ball.sideSpin * RAIL_SPIN_TO_TANGENT + ball.roll * 0.012;
  ball.vx = normalX * nextNormal + tangentX * nextTangent;
  ball.vy = normalY * nextNormal + tangentY * nextTangent;
  ball.sideSpin = ball.sideSpin * RAIL_SPIN_KEEP - tangentVelocity * 0.013;
  ball.roll *= 0.994;
}

function handleWallCollision(ball: PhysicsBall) {
  const minX = RAIL_MARGIN_X + BALL_RADIUS;
  const maxX = TABLE_WIDTH - RAIL_MARGIN_X - BALL_RADIUS;
  const minY = RAIL_MARGIN_Y + BALL_RADIUS;
  const maxY = TABLE_HEIGHT - RAIL_MARGIN_Y - BALL_RADIUS;
  let collided = false;

  if (nearPocket(ball.x, ball.y)) return false;

  if (ball.x < minX) {
    ball.x = minX;
    applyRailResponse(ball, 1, 0);
    collided = true;
  } else if (ball.x > maxX) {
    ball.x = maxX;
    applyRailResponse(ball, -1, 0);
    collided = true;
  }

  if (ball.y < minY) {
    ball.y = minY;
    applyRailResponse(ball, 0, 1);
    collided = true;
  } else if (ball.y > maxY) {
    ball.y = maxY;
    applyRailResponse(ball, 0, -1);
    collided = true;
  }

  return collided;
}

function handlePocket(ball: PhysicsBall, stepScale = 1): number | null {
  if (ball.pocketed) return null;
  const speed = Math.hypot(ball.vx, ball.vy);
  const prevX = ball.x - ball.vx * stepScale;
  const prevY = ball.y - ball.vy * stepScale;

  for (let index = 0; index < POCKETS.length; index += 1) {
    const pocket = POCKETS[index];
    const dx = ball.x - pocket.x;
    const dy = ball.y - pocket.y;
    const dist = Math.hypot(dx, dy);
    const segmentDist = distancePointToSegment(pocket.x, pocket.y, prevX, prevY, ball.x, ball.y);
    const dynamicCaptureRadius = POCKET_CAPTURE_RADIUS + clamp(speed * 0.38, 0, 7);
    const dynamicFunnelRadius = POCKET_FUNNEL_RADIUS + clamp(speed * 0.48, 0, 10);

    if (dist <= dynamicCaptureRadius || segmentDist <= dynamicCaptureRadius - 1.25) {
      ball.pocketed = true;
      ball.vx = 0;
      ball.vy = 0;
      ball.roll = 0;
      ball.sideSpin = 0;
      ball.x = pocket.x;
      ball.y = pocket.y;
      return index + 1;
    }

    if (dist < dynamicFunnelRadius || segmentDist < dynamicFunnelRadius - 1) {
      const nx = dx / (dist || 1);
      const ny = dy / (dist || 1);
      const proximity = clamp(1 - ((Math.min(dist, segmentDist) - dynamicCaptureRadius) / Math.max(1, dynamicFunnelRadius - dynamicCaptureRadius)), 0, 1);
      const pullStrength = (0.018 + clamp(speed * 0.0018, 0, 0.02)) * proximity;
      ball.vx -= nx * pullStrength * Math.max(speed, 0.45);
      ball.vy -= ny * pullStrength * Math.max(speed, 0.45);
    }
  }
  return null;
}

function updateBallMotion(ball: PhysicsBall) {
  if (ball.pocketed) return;
  const speed = Math.hypot(ball.vx, ball.vy);
  const bias = clothBias(ball);
  if (speed <= 0.000001) {
    ball.vx = 0;
    ball.vy = 0;
    ball.sideSpin *= 0.945;
    ball.roll *= 0.95;
    if (Math.abs(ball.sideSpin) < 0.02) ball.sideSpin = 0;
    if (Math.abs(ball.roll) < 0.018) ball.roll = 0;
    return;
  }

  const slidingFactor = clamp(Math.abs(speed - ball.roll) / Math.max(speed, 0.001), 0, 1);
  const lowSpeedBlend = clamp(1 - speed / 1.2, 0, 1);
  let drag = ROLLING_DRAG + (SLIDING_DRAG - ROLLING_DRAG) * slidingFactor;
  if (speed > 8.5) drag = Math.min(drag, HIGH_SPEED_DRAG);
  drag = 1 - (1 - drag) * bias;
  if (lowSpeedBlend > 0) drag = Math.min(0.99945, drag + lowSpeedBlend * 0.0002);
  ball.vx *= drag;
  ball.vy *= drag;

  const postSpeed = Math.hypot(ball.vx, ball.vy);
  const dirX = postSpeed > 0.0001 ? ball.vx / postSpeed : 0;
  const dirY = postSpeed > 0.0001 ? ball.vy / postSpeed : 0;

  ball.roll += (postSpeed - ball.roll) * (ROLL_SYNC_RATE + lowSpeedBlend * 0.05);
  if (ball.roll < -0.035 && postSpeed > 0.11) {
    const slow = Math.max(-0.055, ball.roll * BACKSPIN_DRAG_FACTOR);
    ball.vx += dirX * slow;
    ball.vy += dirY * slow;
  } else if (ball.roll > postSpeed + 0.04 && postSpeed > 0.07) {
    const push = Math.min(0.065, (ball.roll - postSpeed) * OVERSPIN_PUSH_FACTOR);
    ball.vx += dirX * push;
    ball.vy += dirY * push;
  }

  if (Math.abs(ball.sideSpin) > 0.001 && postSpeed > 0.16) {
    rotateVelocity(ball, ball.sideSpin * SPIN_CURVE_FACTOR);
  }

  ball.sideSpin *= SPIN_DECAY;
  ball.roll *= ROLL_KEEP;

  const settledSpeed = Math.hypot(ball.vx, ball.vy);
  const softThreshold = SOFT_STOP_SPEED * (0.965 + (bias - 1) * 4.5);
  const hardThreshold = HARD_STOP_SPEED * (0.95 + (bias - 1) * 5.5);
  if (settledSpeed < softThreshold) {
    const settleFactor = clamp(settledSpeed / Math.max(softThreshold, 0.0001), 0, 1);
    const keep = 0.948 + settleFactor * 0.038;
    ball.vx *= keep;
    ball.vy *= keep;
    ball.sideSpin *= 0.982;
    ball.roll *= 0.978;
  }

  if (settledSpeed < hardThreshold && Math.abs(ball.sideSpin) < SPIN_STOP_SPEED && Math.abs(ball.roll) < ROLL_STOP_SPEED) {
    ball.vx = 0;
    ball.vy = 0;
    ball.sideSpin = 0;
    ball.roll = 0;
  }
}

function resolveCollision(a: PhysicsBall, b: PhysicsBall) {
  if (a.pocketed || b.pocketed) return false;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const distance = Math.hypot(dx, dy) || 0.0001;
  if (distance >= BALL_DIAMETER) return false;

  const nx = dx / distance;
  const ny = dy / distance;
  const overlap = BALL_DIAMETER - distance;
  a.x -= nx * overlap * 0.5;
  a.y -= ny * overlap * 0.5;
  b.x += nx * overlap * 0.5;
  b.y += ny * overlap * 0.5;

  const relativeNormal = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny;
  if (relativeNormal > 0) return true;

  const normalImpulse = -((1 + BALL_BALL_RESTITUTION) * relativeNormal) / 2;
  a.vx -= normalImpulse * nx;
  a.vy -= normalImpulse * ny;
  b.vx += normalImpulse * nx;
  b.vy += normalImpulse * ny;

  const tangentX = -ny;
  const tangentY = nx;
  const relativeTangent = (b.vx - a.vx) * tangentX + (b.vy - a.vy) * tangentY + (b.sideSpin - a.sideSpin) * 0.18;
  const maxTangentImpulse = Math.abs(normalImpulse) * BALL_TANGENT_FRICTION;
  const tangentImpulse = clamp(-relativeTangent * 0.5, -maxTangentImpulse, maxTangentImpulse);
  a.vx -= tangentImpulse * tangentX;
  a.vy -= tangentImpulse * tangentY;
  b.vx += tangentImpulse * tangentX;
  b.vy += tangentImpulse * tangentY;
  a.sideSpin -= tangentImpulse * BALL_SPIN_TRANSFER;
  b.sideSpin += tangentImpulse * BALL_SPIN_TRANSFER;
  a.roll += (Math.hypot(a.vx, a.vy) - a.roll) * 0.28;
  b.roll += (Math.hypot(b.vx, b.vy) - b.roll) * 0.28;
  return true;
}

function groupOfNumber(number: number): BallGroup | null {
  if (number >= 1 && number <= 7) return "solids";
  if (number >= 9 && number <= 15) return "stripes";
  return null;
}

function oppositeGroup(group: BallGroup): BallGroup {
  return group === "solids" ? "stripes" : "solids";
}

function remainingForGroup(balls: PhysicsBall[], group: BallGroup | null) {
  if (!group) return 7;
  return balls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === group).length;
}

function assignGroups(game: GameRecord, shooterUserId: string, group: BallGroup) {
  if (shooterUserId === game.hostUserId) {
    game.hostGroup = group;
    game.guestGroup = oppositeGroup(group);
  } else {
    game.guestGroup = group;
    game.hostGroup = oppositeGroup(group);
  }
}

function currentGroup(game: GameRecord, userId: string): BallGroup | null {
  return userId === game.hostUserId ? game.hostGroup : game.guestGroup;
}

function opponentUserId(game: GameRecord, userId: string) {
  const ids = [game.hostUserId, game.guestUserId].filter((value): value is string => Boolean(value));
  return ids.find((value) => value !== userId) ?? userId;
}

function inferPhase(game: GameRecord): GamePhase {
  if (game.status === "finished") return "finished";
  if (!game.hostGroup || !game.guestGroup) return game.shotSequence === 0 ? "break" : "open_table";
  if (remainingForGroup(game.balls, game.hostGroup) === 0 || remainingForGroup(game.balls, game.guestGroup) === 0) return "eight_ball";
  return "group_play";
}

function legalCuePosition(x: number, y: number, breakOnly = false) {
  const minX = RAIL_MARGIN_X + BALL_RADIUS;
  const maxX = breakOnly ? HEAD_STRING_X - BALL_RADIUS - 6 : TABLE_WIDTH - RAIL_MARGIN_X - BALL_RADIUS;
  const minY = RAIL_MARGIN_Y + BALL_RADIUS;
  const maxY = TABLE_HEIGHT - RAIL_MARGIN_Y - BALL_RADIUS;
  return {
    x: clamp(x, minX, maxX),
    y: clamp(y, minY, maxY),
  };
}

function cuePositionOverlaps(balls: PhysicsBall[], cueX: number, cueY: number) {
  for (const ball of balls) {
    if (ball.number === 0 || ball.pocketed) continue;
    if (Math.hypot(ball.x - cueX, ball.y - cueY) < BALL_DIAMETER + 1) return true;
  }
  return false;
}

function resolveCuePlacement(balls: PhysicsBall[], x: number, y: number, breakOnly = false) {
  const base = legalCuePosition(x, y, breakOnly);
  if (!cuePositionOverlaps(balls, base.x, base.y)) return base;

  for (let radius = 6; radius <= 160; radius += 6) {
    for (let step = 0; step < 24; step += 1) {
      const angle = (Math.PI * 2 * step) / 24;
      const candidate = legalCuePosition(
        base.x + Math.cos(angle) * radius,
        base.y + Math.sin(angle) * radius,
        breakOnly,
      );
      if (!cuePositionOverlaps(balls, candidate.x, candidate.y)) return candidate;
    }
  }

  const fallback = legalCuePosition(DEFAULT_CUE_X, DEFAULT_CUE_Y, breakOnly);
  if (!cuePositionOverlaps(balls, fallback.x, fallback.y)) return fallback;
  return base;
}

function simulateShot(
  game: GameRecord,
  shooterUserId: string,
  angle: number,
  power: number,
  cueX?: number | null,
  cueY?: number | null,
  calledPocket?: number | null,
  spinX = 0,
  spinY = 0,
): ShotOutcome {
  const safeAngle = Number.isFinite(angle) ? angle : 0;
  const balls = cloneBalls(game.balls);
  const cueBall = balls.find((ball) => ball.number === 0) ?? createBall(0, DEFAULT_CUE_X, DEFAULT_CUE_Y);
  if (!balls.some((ball) => ball.number === 0)) balls.unshift(cueBall);

  const breakOnlyPlacement = game.shotSequence === 0;
  if (game.ballInHandUserId === shooterUserId) {
    const position = resolveCuePlacement(
      balls,
      Number.isFinite(cueX ?? NaN) ? Number(cueX) : cueBall.x,
      Number.isFinite(cueY ?? NaN) ? Number(cueY) : cueBall.y,
      breakOnlyPlacement,
    );
    cueBall.pocketed = false;
    cueBall.x = position.x;
    cueBall.y = position.y;
    cueBall.vx = 0;
    cueBall.vy = 0;
    cueBall.roll = 0;
    cueBall.sideSpin = 0;
  } else if (cueBall.pocketed) {
    cueBall.pocketed = false;
    cueBall.x = DEFAULT_CUE_X;
    cueBall.y = DEFAULT_CUE_Y;
    cueBall.vx = 0;
    cueBall.vy = 0;
    cueBall.roll = 0;
    cueBall.sideSpin = 0;
  }

  const shotPower = clamp(Number.isFinite(power) ? power : 0.52, POWER_FLOOR, 1);
  const safeSpinX = clampUnit(Number.isFinite(spinX) ? spinX : 0);
  const safeSpinY = clampUnit(Number.isFinite(spinY) ? spinY : 0);
  const shapedShotPower = shotPower * 0.025 + Math.pow(shotPower, 2.18) * 0.975;
  const shotSpeed = MIN_SHOT_SPEED + shapedShotPower * MAX_SHOT_SPEED;
  cueBall.vx = Math.cos(safeAngle) * shotSpeed;
  cueBall.vy = Math.sin(safeAngle) * shotSpeed;
  cueBall.roll = shotSpeed * (0.5 + safeSpinY * SHOT_ROLL_SPIN_GAIN);
  cueBall.sideSpin = shotSpeed * safeSpinX * SHOT_SIDE_SPIN_GAIN;

  const shooterGroupBefore = currentGroup(game, shooterUserId);
  const openTableBefore = !game.hostGroup || !game.guestGroup;
  const currentTargetBefore = openTableBefore
    ? null
    : remainingForGroup(balls, shooterGroupBefore) === 0
      ? "eight"
      : shooterGroupBefore;

  const frames: GameShotFrame[] = [currentFrame(balls)];
  const pocketedEvents: PocketEvent[] = [];
  let cuePocketed = false;
  let firstHitNumber: number | null = null;
  let railAfterContact = false;
  let hadAnyCollision = false;

  for (let step = 0; step < MAX_STEPS; step += 1) {
    const maxVelocity = balls.reduce((max, ball) => ball.pocketed ? max : Math.max(max, Math.abs(ball.vx), Math.abs(ball.vy)), 0);
    const substeps = clamp(Math.ceil(maxVelocity / 7), 1, MAX_SUBSTEPS);

    for (let substep = 0; substep < substeps; substep += 1) {
      for (const ball of balls) {
        if (ball.pocketed) continue;
        ball.x += ball.vx / substeps;
        ball.y += ball.vy / substeps;
        const bounced = handleWallCollision(ball);
        if (bounced && firstHitNumber !== null) railAfterContact = true;
      }

      for (let index = 0; index < balls.length; index += 1) {
        for (let otherIndex = index + 1; otherIndex < balls.length; otherIndex += 1) {
          const a = balls[index];
          const b = balls[otherIndex];
          const collided = resolveCollision(a, b);
          if (!collided) continue;
          hadAnyCollision = true;
          if (firstHitNumber === null) {
            if (a.number === 0 && b.number !== 0) firstHitNumber = b.number;
            else if (b.number === 0 && a.number !== 0) firstHitNumber = a.number;
          }
        }
      }

      for (const ball of balls) {
        if (ball.pocketed) continue;
        const pocketIndex = handlePocket(ball, 1 / substeps);
        if (pocketIndex !== null) {
          if (ball.number === 0) cuePocketed = true;
          else pocketedEvents.push({ number: ball.number, pocketIndex });
        }
      }
    }

    for (const ball of balls) {
      if (ball.pocketed) continue;
      updateBallMotion(ball);
    }

    const sampleEvery = maxVelocity < 1.15 ? 1 : maxVelocity < 3.4 ? FRAME_SAMPLE_EVERY : FRAME_SAMPLE_EVERY + 1;
    if (step % sampleEvery === 0) frames.push(currentFrame(balls));
    if (balls.every((ball) => !ballIsMoving(ball))) {
      const lastFrame = frames[frames.length - 1];
      const settledFrame = currentFrame(balls);
      if (!lastFrame || JSON.stringify(lastFrame.balls) !== JSON.stringify(settledFrame.balls)) {
        frames.push(settledFrame);
      }
      break;
    }
  }

  const trimmedFrames = trimTrailingSettledFrames(frames);

  const firstHitGroup = groupOfNumber(firstHitNumber ?? 0);
  const pocketedNumbers = pocketedEvents.map((event) => event.number);
  const eightPocketEvent = pocketedEvents.find((event) => event.number === 8) ?? null;
  const pocketedOwnGroup = pocketedNumbers.some((number) => shooterGroupBefore && groupOfNumber(number) === shooterGroupBefore);
  const pocketedOnOpen = pocketedNumbers.find((number) => groupOfNumber(number) !== null) ?? null;

  let foulReason: string | null = null;
  if (cuePocketed) foulReason = "scratch";
  else if (firstHitNumber === null && !hadAnyCollision) foulReason = "miss";
  else if (openTableBefore && firstHitNumber === 8) foulReason = "eight_first";
  else if (!openTableBefore && currentTargetBefore === "eight" && firstHitNumber !== 8) foulReason = "wrong_ball";
  else if (!openTableBefore && currentTargetBefore !== "eight" && firstHitGroup !== currentTargetBefore) foulReason = "wrong_ball";
  else if (firstHitNumber === null) foulReason = "miss";
  else if (!pocketedNumbers.length && !railAfterContact) foulReason = "no_rail";

  if (cuePocketed) {
    const scratchPlacement = resolveCuePlacement(balls, DEFAULT_CUE_X, DEFAULT_CUE_Y, false);
    cueBall.pocketed = false;
    cueBall.x = scratchPlacement.x;
    cueBall.y = scratchPlacement.y;
    cueBall.vx = 0;
    cueBall.vy = 0;
    cueBall.roll = 0;
    cueBall.sideSpin = 0;
  }

  let winnerUserId: string | null = null;
  if (eightPocketEvent) {
    const legalEight = !foulReason && currentTargetBefore === "eight" && calledPocket !== null && eightPocketEvent.pocketIndex === calledPocket;
    winnerUserId = legalEight ? shooterUserId : opponentUserId(game, shooterUserId);
  }

  if (!winnerUserId && openTableBefore && !foulReason && pocketedOnOpen) {
    assignGroups(game, shooterUserId, groupOfNumber(pocketedOnOpen)!);
  }

  const nextTurnUserId = winnerUserId
    ? shooterUserId
    : foulReason
      ? opponentUserId(game, shooterUserId)
      : pocketedOwnGroup || (openTableBefore && pocketedOnOpen !== null)
        ? shooterUserId
        : opponentUserId(game, shooterUserId);

  game.balls = balls;
  game.turnUserId = nextTurnUserId;
  game.shotSequence += 1;
  game.updatedAt = Date.now();
  game.foulReason = foulReason;
  game.calledPocket = currentTargetBefore === "eight" ? calledPocket ?? null : null;

  if (winnerUserId) {
    game.status = "finished";
    game.phase = "finished";
    game.winnerUserId = winnerUserId;
    game.ballInHandUserId = null;
  } else {
    game.status = "waiting_shot";
    game.ballInHandUserId = foulReason ? opponentUserId(game, shooterUserId) : null;
    game.phase = inferPhase(game);
  }

  game.lastShot = {
    seq: game.shotSequence,
    shooterUserId,
    nextTurnUserId,
    pocketedNumbers,
    cuePocketed,
    frames: trimmedFrames,
    createdAt: game.updatedAt,
  };

  return {
    frames: trimmedFrames,
    pocketedNumbers,
    cuePocketed,
    nextTurnUserId,
    firstHitNumber,
    foulReason,
    winnerUserId,
    eightPocket: Boolean(eightPocketEvent),
  };
}

export function startGameForRoom(room: RoomRecord): GameSnapshot {
  const existing = games.get(room.roomId);
  if (existing) return toSnapshot(existing);

  const players = room.players.slice(0, 2);
  const firstTurn = players[Math.floor(Math.random() * players.length)]?.userId ?? room.hostUserId;
  const now = Date.now();

  const game: GameRecord = {
    gameId: makeGameId(),
    roomId: room.roomId,
    hostUserId: room.hostUserId,
    guestUserId: players.find((player) => player.userId !== room.hostUserId)?.userId ?? null,
    tableType: room.tableType,
    stakeChips: room.stakeChips,
    status: "waiting_shot",
    phase: "break",
    turnUserId: firstTurn,
    shotSequence: 0,
    hostGroup: null,
    guestGroup: null,
    ballInHandUserId: firstTurn,
    winnerUserId: null,
    foulReason: null,
    calledPocket: null,
    balls: rackBalls(),
    createdAt: now,
    updatedAt: now,
    lastShot: null,
  };

  games.set(room.roomId, game);
  return toSnapshot(game);
}

export function getGameSnapshot(roomId: string, sinceSeq?: number | null): GameSnapshot | null {
  const game = games.get(roomId);
  if (!game) return null;
  return toSnapshot(game, sinceSeq);
}

export function hasGame(roomId: string) {
  return games.has(roomId);
}

export function removeGame(roomId: string) {
  const existing = games.get(roomId) ?? null;
  games.delete(roomId);
  return existing ? toSnapshot(existing) : null;
}

export function takeShot(
  roomId: string,
  userId: string,
  angle: number,
  power: number,
  cueX?: number | null,
  cueY?: number | null,
  calledPocket?: number | null,
  spinX = 0,
  spinY = 0,
): GameSnapshot | null {
  const game = games.get(roomId);
  if (!game) return null;
  if (game.turnUserId !== userId || game.status === "finished") return toSnapshot(game);
  simulateShot(game, userId, angle, power, cueX, cueY, calledPocket, spinX, spinY);
  return toSnapshot(game);
}
