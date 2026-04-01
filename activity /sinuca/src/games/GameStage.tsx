import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_SIZE = 20;
const BALL_RADIUS = BALL_SIZE / 2;
const TABLE_MIN_X = 64 + BALL_RADIUS;
const TABLE_MAX_X = TABLE_WIDTH - 64 - BALL_RADIUS;
const TABLE_MIN_Y = 48 + BALL_RADIUS;
const TABLE_MAX_Y = TABLE_HEIGHT - 48 - BALL_RADIUS;
const BREAK_MAX_X = TABLE_WIDTH * 0.34;
const DEFAULT_CUE_X = 300;
const DEFAULT_CUE_Y = TABLE_HEIGHT / 2;
const RACK_APEX_X = 910;
const RACK_APEX_Y = TABLE_HEIGHT / 2;
const RACK_ROW_STEP_X = BALL_SIZE * 0.92;
const RACK_SPACING = BALL_SIZE * 1.02;
const POCKETS = [
  { id: 1, x: 66, y: 48 },
  { id: 2, x: TABLE_WIDTH / 2, y: 34 },
  { id: 3, x: TABLE_WIDTH - 66, y: 48 },
  { id: 4, x: 66, y: TABLE_HEIGHT - 48 },
  { id: 5, x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - 34 },
  { id: 6, x: TABLE_WIDTH - 66, y: TABLE_HEIGHT - 48 },
] as const;

const OPENING_RACK = [
  [1],
  [10, 2],
  [12, 8, 14],
  [3, 6, 7, 11],
  [15, 5, 13, 4, 9],
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

type PointerMode = "idle" | "aim" | "place";

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
    1: "#f3d54d",
    2: "#315fe0",
    3: "#d63e3e",
    4: "#6d45c8",
    5: "#ef8b2d",
    6: "#2f9b5a",
    7: "#7d2c25",
    8: "#17191d",
    9: "#f3d54d",
    10: "#315fe0",
    11: "#d63e3e",
    12: "#6d45c8",
    13: "#ef8b2d",
    14: "#2f9b5a",
    15: "#7d2c25",
  };
  return map[number] ?? "#eef4ff";
}

function resolveBallMeta(number: number) {
  if (number === 0) return { label: "", className: "cue", color: "#f7fbff" };
  if (number === 8) return { label: "8", className: "eight", color: ballColor(number) };
  return {
    label: String(number),
    className: number >= 9 ? "stripe" : "solid",
    color: ballColor(number),
  };
}

function frameToDisplayBalls(frameBalls: GameShotFrameBall[], previousBalls: GameBallSnapshot[]) {
  const map = new Map(frameBalls.map((ball) => [ball.id, ball]));
  return previousBalls.map((ball) => {
    const next = map.get(ball.id);
    return next ? { ...ball, x: next.x, y: next.y, pocketed: next.pocketed } : ball;
  });
}

function groupOfNumber(number: number): BallGroup | null {
  if (number >= 1 && number <= 7) return "solids";
  if (number >= 9 && number <= 15) return "stripes";
  return null;
}

function clampCuePosition(x: number, y: number, breakOnly: boolean) {
  return {
    x: Math.min(breakOnly ? BREAK_MAX_X : TABLE_MAX_X, Math.max(TABLE_MIN_X, x)),
    y: Math.min(TABLE_MAX_Y, Math.max(TABLE_MIN_Y, y)),
  };
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

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, onShoot, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [power, setPower] = useState(0.58);
  const [aimAngle, setAimAngle] = useState(0);
  const [pointerMode, setPointerMode] = useState<PointerMode>("idle");
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState<number>(0);
  const [selectedPocket, setSelectedPocket] = useState<number | null>(null);
  const tableRef = useRef<HTMLDivElement | null>(null);
  const powerRailRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef<number>(0);
  const powerMovedRef = useRef(false);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const isHost = currentUserId === room.hostUserId;
  const me = room.players.find((player) => player.userId === currentUserId) ?? (isHost ? host : guest);
  const opponent = room.players.find((player) => player.userId !== currentUserId) ?? (isHost ? guest : host);
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
    if ((game.phase === "break" || game.shotSequence === 0) && visibleNonCue.length < 10) {
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
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;
  const canShoot = Boolean(canInteract && (!needEightCall || selectedPocket !== null));

  useEffect(() => {
    if (!cueBall) return;
    if (game.phase === "break") {
      setAimAngle(0);
      return;
    }
    if (game.turnUserId !== currentUserId) {
      setAimAngle(Math.PI);
    }
  }, [cueBall?.id, cueBall?.x, cueBall?.y, game.phase, game.turnUserId, currentUserId]);

  useEffect(() => {
    if (!needEightCall) setSelectedPocket(null);
  }, [needEightCall]);

  const pointToLocal = (clientX: number, clientY: number) => {
    if (!tableRef.current) return null;
    const rect = tableRef.current.getBoundingClientRect();
    return {
      x: ((clientX - rect.left) / rect.width) * TABLE_WIDTH,
      y: ((clientY - rect.top) / rect.height) * TABLE_HEIGHT,
    };
  };

  const updateAimFromPoint = (clientX: number, clientY: number) => {
    if (!cueBall) return;
    const point = pointToLocal(clientX, clientY);
    if (!point) return;
    setAimAngle(Math.atan2(cueBall.y - point.y, cueBall.x - point.x));
  };

  const updateCuePositionFromPoint = (clientX: number, clientY: number) => {
    if (!cueBall) return;
    const point = pointToLocal(clientX, clientY);
    if (!point) return;
    const next = clampCuePosition(point.x, point.y, game.shotSequence === 0);
    setDisplayBalls((current) => current.map((ball) => (ball.number === 0 ? { ...ball, x: next.x, y: next.y, pocketed: false } : ball)));
  };

  const updatePowerFromClientY = (clientY: number) => {
    if (!powerRailRef.current) return;
    const rect = powerRailRef.current.getBoundingClientRect();
    const ratio = 1 - (clientY - rect.top) / rect.height;
    setPower(Math.max(0.15, Math.min(1, ratio)));
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    const distanceToCue = Math.hypot(point.x - cueBall.x, point.y - cueBall.y);
    if (isBallInHand && distanceToCue <= 42) {
      setPointerMode("place");
      updateCuePositionFromPoint(event.clientX, event.clientY);
      return;
    }
    setPointerMode("aim");
    updateAimFromPoint(event.clientX, event.clientY);
  };

  const handleTablePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerMode === "place") updateCuePositionFromPoint(event.clientX, event.clientY);
    if (pointerMode === "aim") updateAimFromPoint(event.clientX, event.clientY);
  };

  const handleTablePointerUp = () => {
    setPointerMode("idle");
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canShoot) return;
    powerMovedRef.current = false;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.buttons & 1) !== 1) return;
    powerMovedRef.current = true;
    updatePowerFromClientY(event.clientY);
  };

  const handleShoot = async () => {
    if (!canShoot || !cueBall) return;
    await onShoot({
      angle: aimAngle,
      power,
      cueX: isBallInHand ? cueBall.x : null,
      cueY: isBallInHand ? cueBall.y : null,
      calledPocket: needEightCall ? selectedPocket : null,
    });
  };

  const handlePowerUp = (event?: ReactPointerEvent<HTMLDivElement>) => {
    if (event) event.currentTarget.releasePointerCapture?.(event.pointerId);
    if (powerMovedRef.current && canShoot && !shootBusy) void handleShoot();
    powerMovedRef.current = false;
  };

  const leftPlayer = me;
  const rightPlayer = opponent;
  const leftPocketed = myGroup ? 7 - myRemaining : 0;
  const rightPocketed = opponentGroup ? 7 - opponentRemaining : 0;

  const statusText = game.status === "finished"
    ? game.winnerUserId === currentUserId ? "Você venceu" : "Você perdeu"
    : game.foulReason && !animating
      ? `Falta: ${game.foulReason}`
      : isBallInHand
        ? "Bola na mão"
        : animating
          ? `Tacada ${animatingSeq}`
          : isMyTurn
            ? needEightCall ? "Caçapa da 8" : "Sua vez"
            : `Vez de ${cleanName(opponent?.displayName ?? "oponente")}`;

  const phaseText = game.status === "finished"
    ? "Fim"
    : game.phase === "break"
      ? "Break"
      : game.phase === "open_table"
        ? "Mesa aberta"
        : game.phase === "eight_ball"
          ? "Bola 8"
          : myGroup === "solids"
            ? "Lisas"
            : myGroup === "stripes"
              ? "Listradas"
              : "";

  const aimGuide = useMemo(() => {
    if (!cueBall) return null;
    const guideLength = 520;
    const cueOffset = 230 + power * 120;
    return {
      left: cueBall.x,
      top: cueBall.y,
      angle: aimAngle,
      length: guideLength,
      cueOffset,
      ghostX: cueBall.x + Math.cos(aimAngle) * 320,
      ghostY: cueBall.y + Math.sin(aimAngle) * 320,
      ringX: cueBall.x + Math.cos(aimAngle) * 360,
      ringY: cueBall.y + Math.sin(aimAngle) * 360,
    };
  }, [aimAngle, cueBall, power]);

  return (
    <section className="game-ref game-ref--cover">
      <div className="game-ref__top">
        <button
          className="game-ref__menu"
          type="button"
          disabled={exitBusy}
          onClick={onExit}
          aria-label={isHost ? "Fechar sala" : "Sair"}
          title={isHost ? "Fechar sala" : "Sair"}
        >
          {exitBusy ? "…" : "≡"}
        </button>

        <div className={`game-ref__player ${game.turnUserId === leftPlayer?.userId ? "game-ref__player--active" : ""}`}>
          <div className="game-ref__avatar">
            {leftPlayer?.avatarUrl ? <img src={leftPlayer.avatarUrl} alt={cleanName(leftPlayer.displayName)} /> : <span>{playerInitials(leftPlayer)}</span>}
          </div>
          <div className="game-ref__player-body">
            <strong>{cleanName(leftPlayer?.displayName ?? "Jogador")}</strong>
            <div className="game-ref__pips">
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`left-${index}`} className={`game-ref__pip ${index < leftPocketed ? "game-ref__pip--filled" : ""}`} />
              ))}
            </div>
          </div>
        </div>

        <div className="game-ref__status">
          <span className="game-ref__stake">{cueLabel}</span>
          <strong>{statusText}</strong>
          <small>{phaseText || "Pool"}</small>
        </div>

        <div className={`game-ref__player game-ref__player--right ${game.turnUserId === rightPlayer?.userId ? "game-ref__player--active" : ""}`}>
          <div className="game-ref__player-body game-ref__player-body--right">
            <strong>{cleanName(rightPlayer?.displayName ?? "Adversário")}</strong>
            <div className="game-ref__pips game-ref__pips--right">
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`right-${index}`} className={`game-ref__pip ${index < rightPocketed ? "game-ref__pip--filled" : ""}`} />
              ))}
            </div>
          </div>
          <div className="game-ref__avatar">
            {rightPlayer?.avatarUrl ? <img src={rightPlayer.avatarUrl} alt={cleanName(rightPlayer.displayName)} /> : <span>{playerInitials(rightPlayer)}</span>}
          </div>
        </div>
      </div>

      <div className="game-ref__body">
        <div
          ref={powerRailRef}
          className={`game-ref__power ${canShoot ? "game-ref__power--active" : ""}`}
          onPointerDown={handlePowerDown}
          onPointerMove={handlePowerMove}
          onPointerUp={handlePowerUp}
          onPointerLeave={handlePowerUp}
        >
          <div className="game-ref__power-track">
            <div className="game-ref__power-fill" style={{ height: `${Math.round(power * 100)}%` }} />
            <div className="game-ref__power-knob" style={{ bottom: `calc(${Math.round(power * 100)}% - 10px)` }} />
          </div>
          <div className="game-ref__power-value">{Math.round(power * 100)}</div>
        </div>

        <div className="game-ref__table-shell">
          <div className="game-ref__table-area">
            <div
              ref={tableRef}
              className={`pool-ref ${canInteract ? "pool-ref--interactive" : ""}`}
              onPointerDown={handleTablePointerDown}
              onPointerMove={handleTablePointerMove}
              onPointerUp={handleTablePointerUp}
              onPointerLeave={handleTablePointerUp}
            >
              <div className="pool-ref__felt" />
              <div className="pool-ref__head-string" />
              <div className="pool-ref__head-spot" />

              {POCKETS.map((pocket) => (
                <span key={`pocket-${pocket.id}`} className={`pool-ref__pocket pool-ref__pocket--${pocket.id}`} />
              ))}

              {needEightCall && isMyTurn ? POCKETS.map((pocket) => (
                <button
                  key={pocket.id}
                  type="button"
                  className={`pool-ref__pocket-target ${selectedPocket === pocket.id ? "pool-ref__pocket-target--active" : ""}`}
                  style={{ left: `${pocket.x}px`, top: `${pocket.y}px` }}
                  onClick={() => setSelectedPocket(pocket.id)}
                >
                  {pocket.id}
                </button>
              )) : null}

              {aimGuide && !animating && isMyTurn && cueBall ? (
                <>
                  <div
                    className="pool-ref__aim-line"
                    style={{
                      left: `${aimGuide.left}px`,
                      top: `${aimGuide.top}px`,
                      width: `${aimGuide.length}px`,
                      transform: `translateY(-1px) rotate(${aimGuide.angle}rad)`,
                    }}
                  />
                  <div className="pool-ref__aim-ring" style={{ left: `${aimGuide.ringX}px`, top: `${aimGuide.ringY}px` }} />
                  <div className="pool-ref__ghost-dot" style={{ left: `${aimGuide.ghostX}px`, top: `${aimGuide.ghostY}px` }} />
                  <div
                    className="pool-ref__cue"
                    style={{
                      left: `${cueBall.x}px`,
                      top: `${cueBall.y}px`,
                      transform: `translate(-50%, -50%) rotate(${aimGuide.angle}rad) translateX(${-aimGuide.cueOffset}px)`,
                    }}
                  />
                </>
              ) : null}

              {renderBalls.map((ball) => {
                if (ball.pocketed) return null;
                const meta = resolveBallMeta(ball.number);
                return (
                  <div
                    key={ball.id}
                    className={`pool-ref__ball pool-ref__ball--${meta.className}`}
                    style={{
                      left: `${ball.x - BALL_RADIUS}px`,
                      top: `${ball.y - BALL_RADIUS}px`,
                      ["--ball-color" as string]: meta.color,
                    }}
                  >
                    {meta.label ? <span>{meta.label}</span> : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
