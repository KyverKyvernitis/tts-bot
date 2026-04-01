import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { BallGroup, GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const BALL_SIZE = 24;
const BALL_RADIUS = BALL_SIZE / 2;
const TABLE_MIN_X = 54 + BALL_RADIUS;
const TABLE_MAX_X = TABLE_WIDTH - 54 - BALL_RADIUS;
const TABLE_MIN_Y = 44 + BALL_RADIUS;
const TABLE_MAX_Y = TABLE_HEIGHT - 44 - BALL_RADIUS;
const BREAK_MAX_X = TABLE_WIDTH * 0.27;
const POCKETS = [
  { id: 1, x: 48, y: 38 },
  { id: 2, x: TABLE_WIDTH / 2, y: 26 },
  { id: 3, x: TABLE_WIDTH - 48, y: 38 },
  { id: 4, x: 48, y: TABLE_HEIGHT - 38 },
  { id: 5, x: TABLE_WIDTH / 2, y: TABLE_HEIGHT - 26 },
  { id: 6, x: TABLE_WIDTH - 48, y: TABLE_HEIGHT - 38 },
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
    1: "#f4d144",
    2: "#2a5ce0",
    3: "#d83a3d",
    4: "#6c42ca",
    5: "#e48225",
    6: "#2b9b53",
    7: "#8a2f21",
    8: "#16181c",
    9: "#f4d144",
    10: "#2a5ce0",
    11: "#d83a3d",
    12: "#6c42ca",
    13: "#e48225",
    14: "#2b9b53",
    15: "#8a2f21",
  };
  return map[number] ?? "#eef4ff";
}

function resolveBallMeta(number: number) {
  if (number === 0) return { label: "", className: "cue", color: "#f6fbff" };
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
  const currentPlayer = isHost ? host : guest;
  const opponentPlayer = isHost ? guest : host;
  const myGroup = isHost ? game.hostGroup : game.guestGroup;
  const opponentGroup = isHost ? game.guestGroup : game.hostGroup;
  const cueBall = displayBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const isMyTurn = game.turnUserId === currentUserId;
  const canInteract = Boolean(cueBall && isMyTurn && !animating && !shootBusy && game.status !== "finished");
  const isBallInHand = game.ballInHandUserId === currentUserId && canInteract;
  const cueLabel = game.tableType === "casual" ? "Amistoso" : `${game.stakeChips ?? 0}`;
  const isOpenTable = !game.hostGroup || !game.guestGroup;
  const myRemaining = displayBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === myGroup).length;
  const opponentRemaining = displayBalls.filter((ball) => !ball.pocketed && groupOfNumber(ball.number) === opponentGroup).length;
  const needEightCall = !isOpenTable && myGroup !== null && myRemaining === 0;
  const canShoot = Boolean(canInteract && (!needEightCall || selectedPocket !== null));

  useEffect(() => {
    if (animating) return;
    setDisplayBalls(game.balls);
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
        animationRef.current = window.setTimeout(tick, 28) as unknown as number;
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
    if (animationRef.current !== null) {
      window.clearTimeout(animationRef.current);
    }
  }, []);

  useEffect(() => {
    if (!cueBall) return;
    setAimAngle(0);
  }, [cueBall?.id, game.gameId]);

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
    setAimAngle(Math.atan2(point.y - cueBall.y, point.x - cueBall.x));
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
    const clamped = Math.max(0.12, Math.min(1, ratio));
    setPower(clamped);
  };

  const handleTablePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!cueBall || !canInteract) return;
    const point = pointToLocal(event.clientX, event.clientY);
    if (!point) return;
    const distanceToCue = Math.hypot(point.x - cueBall.x, point.y - cueBall.y);
    if (isBallInHand && distanceToCue <= 36) {
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
    if (event) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    }
    if (powerMovedRef.current && canShoot && !shootBusy) {
      void handleShoot();
    }
    powerMovedRef.current = false;
  };

  const hostPocketed = myGroup && isHost
    ? 7 - myRemaining
    : opponentGroup && !isHost
      ? 7 - opponentRemaining
      : displayBalls.filter((ball) => ball.pocketed && ball.number !== 0 && ball.number !== 8 && groupOfNumber(ball.number) === game.hostGroup).length;
  const guestPocketed = myGroup && !isHost
    ? 7 - myRemaining
    : opponentGroup && isHost
      ? 7 - opponentRemaining
      : displayBalls.filter((ball) => ball.pocketed && ball.number !== 0 && ball.number !== 8 && groupOfNumber(ball.number) === game.guestGroup).length;

  const statusText = game.status === "finished"
    ? game.winnerUserId === currentUserId
      ? "Você venceu"
      : "Você perdeu"
    : game.foulReason && !animating
      ? `Falta: ${game.foulReason}`
      : isBallInHand
        ? "Bola na mão"
        : animating
          ? `Tacada ${animatingSeq}`
          : isMyTurn
            ? needEightCall
              ? "Chame a caçapa da 8"
              : "Sua vez"
            : `Vez de ${cleanName((game.turnUserId === host?.userId ? host : guest)?.displayName ?? "oponente")}`;

  const phaseText = game.status === "finished"
    ? "Fim de partida"
    : game.phase === "break"
      ? "Break"
      : game.phase === "open_table"
        ? "Mesa aberta"
        : game.phase === "eight_ball"
          ? "Bola 8"
          : myGroup === "solids"
            ? "Você: lisas"
            : myGroup === "stripes"
              ? "Você: listradas"
              : "Sem grupo";

  const aimGuide = useMemo(() => {
    if (!cueBall) return null;
    return {
      left: cueBall.x,
      top: cueBall.y,
      angle: aimAngle,
      length: 500,
      cueOffset: 240 + power * 120,
      ghostX: cueBall.x + Math.cos(aimAngle) * 240,
      ghostY: cueBall.y + Math.sin(aimAngle) * 240,
      ringX: cueBall.x + Math.cos(aimAngle) * 286,
      ringY: cueBall.y + Math.sin(aimAngle) * 286,
    };
  }, [aimAngle, cueBall, power]);

  return (
    <section className="game-stage-shell game-stage-shell--mobile">
      <div className="game-mobile-topbar">
        <button className="game-mobile-exit" type="button" disabled={exitBusy} onClick={onExit}>
          {exitBusy ? "Saindo..." : (isHost ? "Fechar sala" : "Sair")}
        </button>

        <div className="game-mobile-hud">
          <div className={`game-mobile-player ${game.turnUserId === host?.userId ? "game-mobile-player--active" : ""}`}>
            <div className="game-mobile-player__avatar">
              {host?.avatarUrl ? <img src={host.avatarUrl} alt={cleanName(host.displayName)} /> : <span>{playerInitials(host)}</span>}
            </div>
            <div className="game-mobile-player__info">
              <strong>{cleanName(host?.displayName ?? "Anfitrião")}</strong>
              <div className="game-mobile-pips">
                {Array.from({ length: 7 }).map((_, index) => (
                  <span key={`host-${index}`} className={`game-mobile-pip ${index < hostPocketed ? "game-mobile-pip--filled" : ""}`} />
                ))}
              </div>
            </div>
          </div>

          <div className="game-mobile-center">
            <span className="game-mobile-center__badge">{cueLabel}</span>
            <strong>{statusText}</strong>
            <small>{phaseText}</small>
          </div>

          <div className={`game-mobile-player game-mobile-player--right ${game.turnUserId === guest?.userId ? "game-mobile-player--active" : ""}`}>
            <div className="game-mobile-player__info game-mobile-player__info--right">
              <strong>{cleanName(guest?.displayName ?? "Aguardando")}</strong>
              <div className="game-mobile-pips game-mobile-pips--right">
                {Array.from({ length: 7 }).map((_, index) => (
                  <span key={`guest-${index}`} className={`game-mobile-pip ${index < guestPocketed ? "game-mobile-pip--filled" : ""}`} />
                ))}
              </div>
            </div>
            <div className="game-mobile-player__avatar">
              {guest?.avatarUrl ? <img src={guest.avatarUrl} alt={cleanName(guest.displayName)} /> : <span>{playerInitials(guest)}</span>}
            </div>
          </div>
        </div>
      </div>

      <div className="game-mobile-main">
        <div
          ref={powerRailRef}
          className={`game-power-rail ${canShoot ? "game-power-rail--active" : ""}`}
          onPointerDown={handlePowerDown}
          onPointerMove={handlePowerMove}
          onPointerUp={handlePowerUp}
          onPointerLeave={handlePowerUp}
        >
          <div className="game-power-rail__track">
            <div className="game-power-rail__fill" style={{ height: `${Math.round(power * 100)}%` }} />
            <div className="game-power-rail__knob" style={{ bottom: `calc(${Math.round(power * 100)}% - 10px)` }} />
          </div>
          <div className="game-power-rail__value">{Math.round(power * 100)}</div>
        </div>

        <div className="game-mobile-table-wrap">
          <div className="game-mobile-table-viewport">
            <div
              ref={tableRef}
              className={`pool-table pool-table--mobile ${canInteract ? "pool-table--interactive" : ""}`}
              onPointerDown={handleTablePointerDown}
              onPointerMove={handleTablePointerMove}
              onPointerUp={handleTablePointerUp}
              onPointerLeave={handleTablePointerUp}
            >
              <div className="pool-table__felt" />
              <div className="pool-table__head-string" />
              <div className="pool-table__head-spot" />
              {Array.from({ length: 6 }).map((_, index) => (
                <span key={`pocket-${index}`} className={`pool-pocket pool-pocket--${index + 1}`} />
              ))}

              {needEightCall && isMyTurn ? POCKETS.map((pocket) => (
                <button
                  key={pocket.id}
                  type="button"
                  className={`pool-pocket-target ${selectedPocket === pocket.id ? "pool-pocket-target--active" : ""}`}
                  style={{ left: `${pocket.x}px`, top: `${pocket.y}px` }}
                  onClick={() => setSelectedPocket(pocket.id)}
                >
                  {pocket.id}
                </button>
              )) : null}

              {aimGuide && !animating && isMyTurn && cueBall ? (
                <>
                  <div
                    className="pool-aim-line"
                    style={{
                      left: `${aimGuide.left}px`,
                      top: `${aimGuide.top}px`,
                      width: `${aimGuide.length}px`,
                      transform: `translateY(-1px) rotate(${aimGuide.angle}rad)`,
                    }}
                  />
                  <div className="pool-aim-ring" style={{ left: `${aimGuide.ringX}px`, top: `${aimGuide.ringY}px` }} />
                  <div className="pool-ghost-dot" style={{ left: `${aimGuide.ghostX}px`, top: `${aimGuide.ghostY}px` }} />
                  <div
                    className="pool-cue"
                    style={{
                      left: `${cueBall.x}px`,
                      top: `${cueBall.y}px`,
                      transform: `translate(-50%, -50%) rotate(${aimGuide.angle}rad) translateX(${-aimGuide.cueOffset}px)`,
                    }}
                  />
                </>
              ) : null}

              {displayBalls.map((ball) => {
                if (ball.pocketed) return null;
                const meta = resolveBallMeta(ball.number);
                return (
    <section className="game-ref">
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

        <div className={`game-ref__player ${game.turnUserId === host?.userId ? "game-ref__player--active" : ""}`}>
          <div className="game-ref__avatar">
            {host?.avatarUrl ? <img src={host.avatarUrl} alt={cleanName(host.displayName)} /> : <span>{playerInitials(host)}</span>}
          </div>
          <div className="game-ref__player-body">
            <strong>{cleanName(host?.displayName ?? "Anfitrião")}</strong>
            <div className="game-ref__pips">
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`host-${index}`} className={`game-ref__pip ${index < hostPocketed ? "game-ref__pip--filled" : ""}`} />
              ))}
            </div>
          </div>
        </div>

        <div className="game-ref__status">
          <span className="game-ref__stake">{cueLabel}</span>
          <strong>{statusText}</strong>
          <small>{phaseText}</small>
        </div>

        <div className={`game-ref__player game-ref__player--right ${game.turnUserId === guest?.userId ? "game-ref__player--active" : ""}`}>
          <div className="game-ref__player-body game-ref__player-body--right">
            <strong>{cleanName(guest?.displayName ?? "Aguardando")}</strong>
            <div className="game-ref__pips game-ref__pips--right">
              {Array.from({ length: 7 }).map((_, index) => (
                <span key={`guest-${index}`} className={`game-ref__pip ${index < guestPocketed ? "game-ref__pip--filled" : ""}`} />
              ))}
            </div>
          </div>
          <div className="game-ref__avatar">
            {guest?.avatarUrl ? <img src={guest.avatarUrl} alt={cleanName(guest.displayName)} /> : <span>{playerInitials(guest)}</span>}
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
            <div className="game-ref__power-knob" style={{ bottom: `calc(${Math.round(power * 100)}% - 11px)` }} />
          </div>
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

              {Array.from({ length: 6 }).map((_, index) => (
                <span key={`pocket-${index}`} className={`pool-ref__pocket pool-ref__pocket--${index + 1}`} />
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

              {displayBalls.map((ball) => {
                if (ball.pocketed) return null;
                const meta = resolveBallMeta(ball.number);
                return (
                  <div
                    key={ball.id}
                    className={`pool-ref__ball pool-ref__ball--${meta.className}`}
                    style={{
                      left: `${ball.x - BALL_RADIUS}px`,
                      top: `${ball.y - BALL_RADIUS}px`,
                      ['--ball-color' as string]: meta.color,
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
