import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomPlayer, RoomSnapshot } from "../types/activity";

const TABLE_WIDTH = 1000;
const TABLE_HEIGHT = 560;
const BALL_SIZE = 28;
const BALL_RADIUS = BALL_SIZE / 2;

type ShotInput = { angle: number; power: number };

type Props = {
  room: RoomSnapshot;
  game: GameSnapshot;
  currentUserId: string;
  shootBusy: boolean;
  exitBusy: boolean;
  onShoot: (shot: ShotInput) => Promise<void>;
  onExit: () => void;
};

function cleanName(name: string) {
  return (name || "jogador").replace(/^@+/, "").trim() || "jogador";
}

function playerInitials(player: RoomPlayer | null | undefined) {
  const source = cleanName(player?.displayName ?? "J");
  const pieces = source.split(/\s+/).filter(Boolean).slice(0, 2);
  return pieces.map((piece) => piece[0]?.toUpperCase() ?? "").join("") || source[0]?.toUpperCase() || "J";
}

function resolveBallMeta(number: number) {
  if (number === 0) return { label: "", className: "cue" };
  if (number === 8) return { label: "8", className: "eight" };
  return {
    label: String(number),
    className: number >= 9 ? "stripe" : "solid",
  };
}

function frameToDisplayBalls(frameBalls: GameShotFrameBall[], previousBalls: GameBallSnapshot[]) {
  const map = new Map(frameBalls.map((ball) => [ball.id, ball]));
  return previousBalls.map((ball) => {
    const next = map.get(ball.id);
    return next ? { ...ball, x: next.x, y: next.y, pocketed: next.pocketed } : ball;
  });
}

export default function GameStage({ room, game, currentUserId, shootBusy, exitBusy, onShoot, onExit }: Props) {
  const [displayBalls, setDisplayBalls] = useState<GameBallSnapshot[]>(game.balls);
  const [power, setPower] = useState(0.62);
  const [aimAngle, setAimAngle] = useState(0);
  const [aiming, setAiming] = useState(false);
  const [powerDragging, setPowerDragging] = useState(false);
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState<number>(0);
  const tableRef = useRef<HTMLDivElement | null>(null);
  const powerRailRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef<number>(0);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const currentTurnName = game.turnUserId === host?.userId ? host?.displayName : guest?.displayName ?? host?.displayName;
  const cueBall = displayBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const isMyTurn = game.turnUserId === currentUserId;
  const isHost = currentUserId === room.hostUserId;
  const canShoot = Boolean(cueBall && isMyTurn && !animating && !shootBusy);
  const cueLabel = game.tableType === "casual" ? "Amistoso" : `${game.stakeChips ?? 0}`;

  useEffect(() => {
    if (animating) return;
    setDisplayBalls(game.balls);
  }, [animating, game.balls, game.shotSequence]);

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
  }, [cueBall, game.gameId]);

  const updateAimFromPoint = (clientX: number, clientY: number) => {
    if (!cueBall || !tableRef.current) return;
    const rect = tableRef.current.getBoundingClientRect();
    const scaleX = TABLE_WIDTH / rect.width;
    const scaleY = TABLE_HEIGHT / rect.height;
    const localX = (clientX - rect.left) * scaleX;
    const localY = (clientY - rect.top) * scaleY;
    setAimAngle(Math.atan2(localY - cueBall.y, localX - cueBall.x));
  };

  const updatePowerFromClientY = (clientY: number) => {
    if (!powerRailRef.current) return;
    const rect = powerRailRef.current.getBoundingClientRect();
    const ratio = 1 - (clientY - rect.top) / rect.height;
    const clamped = Math.max(0.18, Math.min(1, ratio));
    setPower(clamped);
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canShoot) return;
    setAiming(true);
    updateAimFromPoint(event.clientX, event.clientY);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!aiming) return;
    updateAimFromPoint(event.clientX, event.clientY);
  };

  const handlePointerUp = () => {
    setAiming(false);
  };

  const handlePowerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!canShoot) return;
    setPowerDragging(true);
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!powerDragging) return;
    updatePowerFromClientY(event.clientY);
  };

  const handlePowerUp = () => {
    setPowerDragging(false);
  };

  const handleShoot = async () => {
    if (!canShoot) return;
    await onShoot({ angle: aimAngle, power });
  };

  const aimGuide = useMemo(() => {
    if (!cueBall) return null;
    const cueOffset = 74 + power * 88;
    return {
      left: cueBall.x,
      top: cueBall.y,
      angle: aimAngle,
      length: 430,
      cueOffset,
      ghostX: cueBall.x + Math.cos(aimAngle) * 226,
      ghostY: cueBall.y + Math.sin(aimAngle) * 226,
      markerX: cueBall.x + Math.cos(aimAngle) * 270,
      markerY: cueBall.y + Math.sin(aimAngle) * 270,
    };
  }, [aimAngle, cueBall, power]);

  const statusText = animating
    ? `Tacada ${animatingSeq}`
    : canShoot
      ? "Sua vez"
      : isMyTurn
        ? "Bolas em movimento"
        : `Vez de ${cleanName(currentTurnName ?? "jogador")}`;

  const hostPocketed = displayBalls.filter((ball) => ball.pocketed && ball.number !== 0 && ball.number !== 8).slice(0, 7).length;
  const guestPocketed = Math.max(0, displayBalls.filter((ball) => ball.pocketed && ball.number !== 0 && ball.number !== 8).length - hostPocketed);

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
            <small>{game.shotSequence === 0 ? "Break" : `Tacada ${game.shotSequence}`}</small>
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
            <div className="game-power-rail__knob" style={{ bottom: `calc(${Math.round(power * 100)}% - 12px)` }} />
          </div>
          <div className="game-power-rail__value">{Math.round(power * 100)}</div>
        </div>

        <div className="game-mobile-table-wrap">
          <div className="game-mobile-table-viewport">
            <div
              ref={tableRef}
              className={`pool-table pool-table--mobile ${canShoot ? "pool-table--interactive" : ""}`}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerLeave={handlePointerUp}
            >
              <div className="pool-table__felt" />
              <div className="pool-table__head-string" />
              <div className="pool-table__head-spot" />
              {Array.from({ length: 6 }).map((_, index) => (
                <span key={`pocket-${index}`} className={`pool-pocket pool-pocket--${index + 1}`} />
              ))}

              {aimGuide && !animating && isMyTurn ? (
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
                  <div
                    className="pool-aim-ring"
                    style={{ left: `${aimGuide.markerX}px`, top: `${aimGuide.markerY}px` }}
                  />
                  <div
                    className="pool-ghost-dot"
                    style={{ left: `${aimGuide.ghostX}px`, top: `${aimGuide.ghostY}px` }}
                  />
                  <div
                    className="pool-cue"
                    style={{
                      left: `${aimGuide.left}px`,
                      top: `${aimGuide.top}px`,
                      transform: `translate(-100%, -50%) rotate(${aimGuide.angle}rad) translateX(${-aimGuide.cueOffset}px)`,
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
                    className={`pool-ball pool-ball--${meta.className}`}
                    style={{ left: `${ball.x - BALL_RADIUS}px`, top: `${ball.y - BALL_RADIUS}px` }}
                  >
                    {meta.label ? <span>{meta.label}</span> : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <button className="game-shoot-fab" type="button" disabled={!canShoot || shootBusy} onClick={handleShoot}>
          {shootBusy ? "..." : "Tacar"}
        </button>
      </div>
    </section>
  );
}
