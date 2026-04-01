import type { GameBallSnapshot, GameShotFrame, GameShotSnapshot, GameSnapshot } from "./messages.js";
import type { RoomRecord } from "./rooms.js";

const TABLE_WIDTH = 1000;
const TABLE_HEIGHT = 560;
const BALL_RADIUS = 14;
const BALL_DIAMETER = BALL_RADIUS * 2;
const POCKET_RADIUS = 30;
const RAIL_MARGIN_X = 58;
const RAIL_MARGIN_Y = 52;
const DEFAULT_CUE_X = 250;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const MAX_SHOT_SPEED = 26;
const MIN_SPEED = 0.04;
const FRICTION = 0.992;
const MAX_STEPS = 900;
const FRAME_SAMPLE_EVERY = 2;

const POCKETS = [
  { x: RAIL_MARGIN_X - 8, y: RAIL_MARGIN_Y - 8 },
  { x: TABLE_WIDTH / 2, y: RAIL_MARGIN_Y - 14 },
  { x: TABLE_WIDTH - RAIL_MARGIN_X + 8, y: RAIL_MARGIN_Y - 8 },
  { x: RAIL_MARGIN_X - 8, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 8 },
  { x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 14 },
  { x: TABLE_WIDTH - RAIL_MARGIN_X + 8, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 8 },
] as const;

interface PhysicsBall extends GameBallSnapshot {
  vx: number;
  vy: number;
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

function createBall(number: number, x: number, y: number): PhysicsBall {
  return {
    id: `ball-${number}`,
    number,
    x,
    y,
    pocketed: false,
    vx: 0,
    vy: 0,
  };
}

function rackBalls(): PhysicsBall[] {
  const balls: PhysicsBall[] = [createBall(0, DEFAULT_CUE_X, DEFAULT_CUE_Y)];
  const apexX = 720;
  const apexY = TABLE_HEIGHT / 2;
  const spacing = BALL_DIAMETER * 1.02;
  const rackOrder = [1, 9, 2, 10, 8, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15];
  let index = 0;

  for (let row = 0; row < 5; row += 1) {
    const rowX = apexX + row * (BALL_DIAMETER * 0.92);
    const rowYOffset = row * (spacing / 2);
    for (let col = 0; col <= row; col += 1) {
      const rowY = apexY - rowYOffset + col * spacing;
      balls.push(createBall(rackOrder[index], rowX, rowY));
      index += 1;
    }
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

function toSnapshot(game: GameRecord, sinceSeq?: number | null): GameSnapshot {
  return {
    ...game,
    balls: game.balls.map(({ vx: _vx, vy: _vy, ...ball }) => ball),
    lastShot: game.lastShot && (!sinceSeq || game.lastShot.seq > sinceSeq) ? game.lastShot : null,
  };
}

function currentFrame(balls: PhysicsBall[]): GameShotFrame {
  return { balls: toFrameBalls(balls) };
}

function ballIsMoving(ball: PhysicsBall) {
  return !ball.pocketed && (Math.abs(ball.vx) > MIN_SPEED || Math.abs(ball.vy) > MIN_SPEED);
}

function handleWallCollision(ball: PhysicsBall) {
  const minX = RAIL_MARGIN_X + BALL_RADIUS;
  const maxX = TABLE_WIDTH - RAIL_MARGIN_X - BALL_RADIUS;
  const minY = RAIL_MARGIN_Y + BALL_RADIUS;
  const maxY = TABLE_HEIGHT - RAIL_MARGIN_Y - BALL_RADIUS;

  if (ball.x < minX) {
    ball.x = minX;
    ball.vx *= -0.98;
  } else if (ball.x > maxX) {
    ball.x = maxX;
    ball.vx *= -0.98;
  }

  if (ball.y < minY) {
    ball.y = minY;
    ball.vy *= -0.98;
  } else if (ball.y > maxY) {
    ball.y = maxY;
    ball.vy *= -0.98;
  }
}

function handlePocket(ball: PhysicsBall) {
  if (ball.pocketed) return false;
  for (const pocket of POCKETS) {
    const dx = ball.x - pocket.x;
    const dy = ball.y - pocket.y;
    if (Math.hypot(dx, dy) <= POCKET_RADIUS - 3) {
      ball.pocketed = true;
      ball.vx = 0;
      ball.vy = 0;
      ball.x = pocket.x;
      ball.y = pocket.y;
      return true;
    }
  }
  return false;
}

function resolveCollision(a: PhysicsBall, b: PhysicsBall) {
  if (a.pocketed || b.pocketed) return;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const distance = Math.hypot(dx, dy) || 0.0001;
  if (distance >= BALL_DIAMETER) return;

  const nx = dx / distance;
  const ny = dy / distance;
  const overlap = BALL_DIAMETER - distance;
  a.x -= nx * overlap * 0.5;
  a.y -= ny * overlap * 0.5;
  b.x += nx * overlap * 0.5;
  b.y += ny * overlap * 0.5;

  const relativeVelocity = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny;
  if (relativeVelocity > 0) return;

  const impulse = -relativeVelocity;
  a.vx -= impulse * nx;
  a.vy -= impulse * ny;
  b.vx += impulse * nx;
  b.vy += impulse * ny;
}

function simulateShot(game: GameRecord, shooterUserId: string, angle: number, power: number) {
  const safeAngle = Number.isFinite(angle) ? angle : 0;
  const balls = cloneBalls(game.balls);
  let cueBall = balls.find((ball) => ball.number === 0);
  if (!cueBall) {
    cueBall = createBall(0, DEFAULT_CUE_X, DEFAULT_CUE_Y);
    balls.unshift(cueBall);
  }
  if (cueBall.pocketed) {
    cueBall.pocketed = false;
    cueBall.x = DEFAULT_CUE_X;
    cueBall.y = DEFAULT_CUE_Y;
  }

  const shotPower = clamp(Number.isFinite(power) ? power : 0.62, 0.18, 1);
  const shotSpeed = 8 + shotPower * MAX_SHOT_SPEED;
  cueBall.vx = Math.cos(safeAngle) * shotSpeed;
  cueBall.vy = Math.sin(safeAngle) * shotSpeed;

  const frames: GameShotFrame[] = [currentFrame(balls)];
  const pocketedNumbers = new Set<number>();
  let cuePocketed = false;

  for (let step = 0; step < MAX_STEPS; step += 1) {
    for (const ball of balls) {
      if (ball.pocketed) continue;
      ball.x += ball.vx;
      ball.y += ball.vy;
      handleWallCollision(ball);
    }

    for (let index = 0; index < balls.length; index += 1) {
      for (let otherIndex = index + 1; otherIndex < balls.length; otherIndex += 1) {
        resolveCollision(balls[index], balls[otherIndex]);
      }
    }

    for (const ball of balls) {
      if (ball.pocketed) continue;
      if (handlePocket(ball)) {
        if (ball.number === 0) cuePocketed = true;
        else pocketedNumbers.add(ball.number);
        continue;
      }
      ball.vx *= FRICTION;
      ball.vy *= FRICTION;
      if (Math.abs(ball.vx) < MIN_SPEED) ball.vx = 0;
      if (Math.abs(ball.vy) < MIN_SPEED) ball.vy = 0;
    }

    if (step % FRAME_SAMPLE_EVERY === 0) {
      frames.push(currentFrame(balls));
    }

    if (balls.every((ball) => !ballIsMoving(ball))) {
      break;
    }
  }

  if (cuePocketed) {
    cueBall.pocketed = false;
    cueBall.x = DEFAULT_CUE_X;
    cueBall.y = DEFAULT_CUE_Y;
    cueBall.vx = 0;
    cueBall.vy = 0;
  }

  const playerIds = [game.hostUserId, game.guestUserId].filter((value): value is string => Boolean(value));
  const currentIndex = playerIds.findIndex((value) => value === shooterUserId);
  const nextTurnUserId = playerIds[(currentIndex + 1) % playerIds.length] ?? shooterUserId;
  const nextShotSeq = game.shotSequence + 1;
  const now = Date.now();

  game.balls = balls;
  game.turnUserId = nextTurnUserId;
  game.shotSequence = nextShotSeq;
  game.updatedAt = now;
  game.lastShot = {
    seq: nextShotSeq,
    shooterUserId,
    nextTurnUserId,
    pocketedNumbers: [...pocketedNumbers],
    cuePocketed,
    frames,
    createdAt: now,
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
    turnUserId: firstTurn,
    shotSequence: 0,
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

export function takeShot(roomId: string, userId: string, angle: number, power: number): GameSnapshot | null {
  const game = games.get(roomId);
  if (!game) return null;
  if (game.turnUserId !== userId) return toSnapshot(game);
  simulateShot(game, userId, angle, power);
  return toSnapshot(game);
}
