import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { GameBallSnapshot, GameShotFrameBall, GameSnapshot, RoomSnapshot } from "../types/activity";

const TABLE_WIDTH = 1000;
const TABLE_HEIGHT = 560;
const BALL_SIZE = 28;
const BALL_RADIUS = BALL_SIZE / 2;

type Props = {
  room: RoomSnapshot;
  game: GameSnapshot;
  currentUserId: string;
  shootBusy: boolean;
  exitBusy: boolean;
  onShoot: (shot: { angle: number; power: number }) => Promise<void>;
  onExit: () => void;
};

function cleanName(name: string) {
  return (name || "jogador").replace(/^@+/, "").trim() || "jogador";
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
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState<number>(0);
  const tableRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef<number>(0);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0];
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const currentTurnName = game.turnUserId === host?.userId ? host?.displayName : guest?.displayName ?? host?.displayName;
  const cueBall = displayBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const isMyTurn = game.turnUserId === currentUserId;
  const canShoot = Boolean(cueBall && isMyTurn && !animating && !shootBusy);

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
  }, [game.gameId]);

  const aimGuide = useMemo(() => {
    if (!cueBall) return null;
    const length = 350;
    return {
      left: cueBall.x,
      top: cueBall.y,
      angle: aimAngle,
      length,
    };
  }, [aimAngle, cueBall]);

  const updateAimFromPoint = (clientX: number, clientY: number) => {
    if (!cueBall || !tableRef.current) return;
    const rect = tableRef.current.getBoundingClientRect();
    const scaleX = TABLE_WIDTH / rect.width;
    const scaleY = TABLE_HEIGHT / rect.height;
    const localX = (clientX - rect.left) * scaleX;
    const localY = (clientY - rect.top) * scaleY;
    setAimAngle(Math.atan2(localY - cueBall.y, localX - cueBall.x));
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

  const handleShoot = async () => {
    if (!canShoot) return;
    await onShoot({ angle: aimAngle, power });
  };

  return (
    <section className="game-stage-shell">
      <div className="game-topbar">
        <button className="chip-button chip-button--back" type="button" disabled={exitBusy} onClick={onExit}>
          {exitBusy ? "Saindo..." : (room.hostUserId === currentUserId ? "Fechar sala" : "Sair")}
        </button>
        <div className="game-topbar__status">
          <span className="room-stage__top-chip">{game.tableType === "casual" ? "Amistoso" : game.stakeChips ?? 0}</span>
          <span className="room-ready-badge room-ready-badge--ready">Fase 1</span>
        </div>
      </div>

      <div className="game-hud">
        <div className={`game-hud__player ${game.turnUserId === host?.userId ? "game-hud__player--active" : ""}`}>
          <span className="game-hud__label">Anfitrião</span>
          <strong>{cleanName(host?.displayName ?? "Anfitrião")}</strong>
        </div>
        <div className="game-hud__center">
          <span className="game-hud__turn">{animating ? "Bolas em movimento" : `Vez de ${cleanName(currentTurnName ?? "jogador")}`}</span>
          <small>{game.shotSequence === 0 ? "Break inicial" : `Tacada ${game.shotSequence}`}</small>
        </div>
        <div className={`game-hud__player ${game.turnUserId === guest?.userId ? "game-hud__player--active" : ""}`}>
          <span className="game-hud__label">Adversário</span>
          <strong>{cleanName(guest?.displayName ?? "Aguardando")}</strong>
        </div>
      </div>

      <div className="game-layout">
        <div
          ref={tableRef}
          className={`pool-table ${canShoot ? "pool-table--interactive" : ""}`}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerUp}
        >
          <div className="pool-table__felt" />
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
                className="pool-cue"
                style={{
                  left: `${aimGuide.left}px`,
                  top: `${aimGuide.top}px`,
                  transform: `translate(-100%, -50%) rotate(${aimGuide.angle}rad) translateX(${-52 - power * 56}px)`,
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

        <aside className="game-controls">
          <div className="game-controls__card">
            <span className="game-controls__eyebrow">Taco</span>
            <strong>{canShoot ? "Mire e bata" : (animating ? `Tacada ${animatingSeq}` : (isMyTurn ? "Aguardando branca" : "Espere sua vez"))}</strong>
            <small>Arraste na mesa para girar a mira. A barra ajusta a força da tacada.</small>
          </div>

          <label className="power-slider">
            <span>Força</span>
            <input type="range" min="18" max="100" value={Math.round(power * 100)} onChange={(event) => setPower(Number(event.target.value) / 100)} disabled={!canShoot} />
            <strong>{Math.round(power * 100)}%</strong>
          </label>

          <button className="primary-button game-controls__shoot" type="button" disabled={!canShoot || shootBusy} onClick={handleShoot}>
            {shootBusy ? "Tacando..." : (isMyTurn ? "Tacar" : "Aguardando adversário")}
          </button>

          <div className="game-controls__legend">
            <span className="game-controls__legend-ball game-controls__legend-ball--solid">Lisas</span>
            <span className="game-controls__legend-ball game-controls__legend-ball--stripe">Listradas</span>
            <span className="game-controls__legend-ball game-controls__legend-ball--eight">8</span>
          </div>
        </aside>
      </div>
    </section>
  );
}
