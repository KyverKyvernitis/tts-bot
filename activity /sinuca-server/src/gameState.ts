import type { BallGroup, GameBallSnapshot, GamePhase, GameShotFrame, GameShotSnapshot, GameSnapshot } from "./messages.js";
import type { RoomRecord } from "./rooms.js";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_RADIUS = 12;
const BALL_DIAMETER = BALL_RADIUS * 2;
const POCKET_RADIUS = 34;
const RAIL_MARGIN_X = 54;
const RAIL_MARGIN_Y = 44;
const DEFAULT_CUE_X = 300;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const MAX_SHOT_SPEED = 24;
const MIN_SPEED = 0.05;
const FRICTION = 0.992;
const MAX_STEPS = 960;
const FRAME_SAMPLE_EVERY = 2;

const POCKETS = [
  { x: RAIL_MARGIN_X - 6, y: RAIL_MARGIN_Y - 6 },
  { x: TABLE_WIDTH / 2, y: RAIL_MARGIN_Y - 14 },
  { x: TABLE_WIDTH - RAIL_MARGIN_X + 6, y: RAIL_MARGIN_Y - 6 },
  { x: RAIL_MARGIN_X - 6, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 6 },
  { x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 14 },
  { x: TABLE_WIDTH - RAIL_MARGIN_X + 6, y: TABLE_HEIGHT - RAIL_MARGIN_Y + 6 },
] as const;

interface PhysicsBall extends GameBallSnapshot {
  vx: number;
  vy: number;
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
  const apexX = 870;
  const apexY = TABLE_HEIGHT / 2;
  const rowStepX = BALL_DIAMETER * 0.88;
  const spacing = BALL_DIAMETER * 1.01;
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
      balls.push(createBall(number, rowX, startY + index * spacing));
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

function toSnapshot(game: GameRecord, sinceSeq?: number | null): GameSnapshot {
  return {
    ...game,
    balls: game.balls.map(({ vx: _vx, vy: _vy, ...ball }) => ball),
    lastShot: game.lastShot && (!sinceSeq || game.lastShot.seq > sinceSeq) ? game.lastShot : null,
  };
}

function ballIsMoving(ball: PhysicsBall) {
  return !ball.pocketed && (Math.abs(ball.vx) > MIN_SPEED || Math.abs(ball.vy) > MIN_SPEED);
}

function handleWallCollision(ball: PhysicsBall) {
  const minX = RAIL_MARGIN_X + BALL_RADIUS;
  const maxX = TABLE_WIDTH - RAIL_MARGIN_X - BALL_RADIUS;
  const minY = RAIL_MARGIN_Y + BALL_RADIUS;
  const maxY = TABLE_HEIGHT - RAIL_MARGIN_Y - BALL_RADIUS;
  let collided = false;

  if (ball.x < minX) {
    ball.x = minX;
    ball.vx *= -0.985;
    collided = true;
  } else if (ball.x > maxX) {
    ball.x = maxX;
    ball.vx *= -0.985;
    collided = true;
  }

  if (ball.y < minY) {
    ball.y = minY;
    ball.vy *= -0.985;
    collided = true;
  } else if (ball.y > maxY) {
    ball.y = maxY;
    ball.vy *= -0.985;
    collided = true;
  }

  return collided;
}

function handlePocket(ball: PhysicsBall): number | null {
  if (ball.pocketed) return null;
  for (let index = 0; index < POCKETS.length; index += 1) {
    const pocket = POCKETS[index];
    const dx = ball.x - pocket.x;
    const dy = ball.y - pocket.y;
    if (Math.hypot(dx, dy) <= POCKET_RADIUS - 3) {
      ball.pocketed = true;
      ball.vx = 0;
      ball.vy = 0;
      ball.x = pocket.x;
      ball.y = pocket.y;
      return index + 1;
    }
  }
  return null;
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

  const relativeVelocity = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny;
  if (relativeVelocity > 0) return true;

  const impulse = -relativeVelocity;
  a.vx -= impulse * nx;
  a.vy -= impulse * ny;
  b.vx += impulse * nx;
  b.vy += impulse * ny;
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
  const maxX = breakOnly ? TABLE_WIDTH * 0.27 : TABLE_WIDTH - RAIL_MARGIN_X - BALL_RADIUS;
  const minY = RAIL_MARGIN_Y + BALL_RADIUS;
  const maxY = TABLE_HEIGHT - RAIL_MARGIN_Y - BALL_RADIUS;
  return {
    x: clamp(x, minX, maxX),
    y: clamp(y, minY, maxY),
  };
}

function simulateShot(
  game: GameRecord,
  shooterUserId: string,
  angle: number,
  power: number,
  cueX?: number | null,
  cueY?: number | null,
  calledPocket?: number | null,
): ShotOutcome {
  const safeAngle = Number.isFinite(angle) ? angle : 0;
  const balls = cloneBalls(game.balls);
  const cueBall = balls.find((ball) => ball.number === 0) ?? createBall(0, DEFAULT_CUE_X, DEFAULT_CUE_Y);
  if (!balls.some((ball) => ball.number === 0)) balls.unshift(cueBall);

  const breakOnlyPlacement = game.shotSequence === 0;
  if (game.ballInHandUserId === shooterUserId) {
    const position = legalCuePosition(
      Number.isFinite(cueX ?? NaN) ? Number(cueX) : cueBall.x,
      Number.isFinite(cueY ?? NaN) ? Number(cueY) : cueBall.y,
      breakOnlyPlacement,
    );
    cueBall.pocketed = false;
    cueBall.x = position.x;
    cueBall.y = position.y;
    cueBall.vx = 0;
    cueBall.vy = 0;
  } else if (cueBall.pocketed) {
    cueBall.pocketed = false;
    cueBall.x = DEFAULT_CUE_X;
    cueBall.y = DEFAULT_CUE_Y;
    cueBall.vx = 0;
    cueBall.vy = 0;
  }

  const shotPower = clamp(Number.isFinite(power) ? power : 0.6, 0.12, 1);
  const shotSpeed = 4 + shotPower * MAX_SHOT_SPEED;
  cueBall.vx = Math.cos(safeAngle) * shotSpeed;
  cueBall.vy = Math.sin(safeAngle) * shotSpeed;

  const frames: GameShotFrame[] = [currentFrame(balls)];
  const pocketedEvents: PocketEvent[] = [];
  let cuePocketed = false;
  let firstHitNumber: number | null = null;
  let railAfterContact = false;
  let hadAnyCollision = false;

  for (let step = 0; step < MAX_STEPS; step += 1) {
    for (const ball of balls) {
      if (ball.pocketed) continue;
      ball.x += ball.vx;
      ball.y += ball.vy;
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
      const pocketIndex = handlePocket(ball);
      if (pocketIndex !== null) {
        if (ball.number === 0) cuePocketed = true;
        else pocketedEvents.push({ number: ball.number, pocketIndex });
        continue;
      }
      ball.vx *= FRICTION;
      ball.vy *= FRICTION;
      if (Math.abs(ball.vx) < MIN_SPEED) ball.vx = 0;
      if (Math.abs(ball.vy) < MIN_SPEED) ball.vy = 0;
    }

    if (step % FRAME_SAMPLE_EVERY === 0) frames.push(currentFrame(balls));
    if (balls.every((ball) => !ballIsMoving(ball))) break;
  }

  const shooterGroup = currentGroup(game, shooterUserId);
  const openTable = !game.hostGroup || !game.guestGroup;
  const currentTarget = openTable ? null : remainingForGroup(balls, shooterGroup) === 0 ? "eight" : shooterGroup;
  const firstHitGroup = groupOfNumber(firstHitNumber ?? 0);
  const pocketedNumbers = pocketedEvents.map((event) => event.number);
  const eightPocketEvent = pocketedEvents.find((event) => event.number === 8) ?? null;
  const pocketedOwnGroup = pocketedNumbers.some((number) => shooterGroup && groupOfNumber(number) === shooterGroup);
  const pocketedOnOpen = pocketedNumbers.find((number) => groupOfNumber(number) !== null) ?? null;

  let foulReason: string | null = null;
  if (cuePocketed) foulReason = "scratch";
  else if (firstHitNumber === null && !hadAnyCollision) foulReason = "miss";
  else if (openTable && firstHitNumber === 8) foulReason = "eight_first";
  else if (!openTable && currentTarget === "eight" && firstHitNumber !== 8) foulReason = "wrong_ball";
  else if (!openTable && currentTarget !== "eight" && firstHitGroup !== currentTarget) foulReason = "wrong_ball";
  else if (firstHitNumber === null) foulReason = "miss";
  else if (!pocketedNumbers.length && !railAfterContact) foulReason = "no_rail";

  let winnerUserId: string | null = null;
  if (eightPocketEvent) {
    const legalEight = !foulReason && currentTarget === "eight" && calledPocket !== null && eightPocketEvent.pocketIndex === calledPocket;
    winnerUserId = legalEight ? shooterUserId : opponentUserId(game, shooterUserId);
  }

  if (!winnerUserId && openTable && !foulReason && pocketedOnOpen) {
    assignGroups(game, shooterUserId, groupOfNumber(pocketedOnOpen)!);
  }

  const nextTurnUserId = winnerUserId
    ? shooterUserId
    : foulReason
      ? opponentUserId(game, shooterUserId)
      : pocketedOwnGroup || (openTable && pocketedOnOpen !== null)
        ? shooterUserId
        : opponentUserId(game, shooterUserId);

  game.balls = balls;
  game.turnUserId = nextTurnUserId;
  game.shotSequence += 1;
  game.updatedAt = Date.now();
  game.foulReason = foulReason;
  game.calledPocket = currentTarget === "eight" ? calledPocket ?? null : null;

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
    frames,
    createdAt: game.updatedAt,
  };

  return {
    frames,
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
): GameSnapshot | null {
  const game = games.get(roomId);
  if (!game) return null;
  if (game.turnUserId !== userId || game.status === "finished") return toSnapshot(game);
  simulateShot(game, userId, angle, power, cueX, cueY, calledPocket);
  return toSnapshot(game);
}
