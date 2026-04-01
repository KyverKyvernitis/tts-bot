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
  const [animating, setAnimating] = useState(false);
  const [animatingSeq, setAnimatingSeq] = useState<number>(0);
  const tableRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const lastAnimatedSeqRef = useRef<number>(0);

  const host = room.players.find((player) => player.userId === room.hostUserId) ?? room.players[0] ?? null;
  const guest = room.players.find((player) => player.userId !== room.hostUserId) ?? null;
  const currentTurnName = game.turnUserId === host?.userId ? host?.displayName : guest?.displayName ?? host?.displayName;
  const cueBall = displayBalls.find((ball) => ball.number === 0 && !ball.pocketed) ?? null;
  const isMyTurn = game.turnUserId === currentUserId;
  const isHost = currentUserId === room.hostUserId;
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
  }, [cueBall, game.gameId]);

  const aimGuide = useMemo(() => {
    if (!cueBall) return null;
    return {
      left: cueBall.x,
      top: cueBall.y,
      angle: aimAngle,
      length: 350,
      ghostX: cueBall.x + Math.cos(aimAngle) * 215,
      ghostY: cueBall.y + Math.sin(aimAngle) * 215,
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

  const tableLabel = game.tableType === "casual" ? "Amistoso" : `Entrada ${game.stakeChips ?? 0}`;
  const statusText = animating
    ? `Tacada ${animatingSeq} em andamento`
    : canShoot
      ? "Sua vez de jogar"
      : isMyTurn
        ? "Aguardando a branca voltar"
        : `Vez de ${cleanName(currentTurnName ?? "jogador")}`;

  return (
    <section className="game-stage-shell game-stage-shell--reboot">
      <div className="game-topbar game-topbar--reboot">
        <button className="chip-button chip-button--back game-exit-button" type="button" disabled={exitBusy} onClick={onExit}>
          {exitBusy ? "Saindo..." : (isHost ? "Fechar sala" : "Sair")}
        </button>
        <div className="game-topbar__status game-topbar__status--reboot">
          <span className="game-chip">{tableLabel}</span>
          <span className={`game-chip ${canShoot ? "game-chip--active" : ""}`}>{statusText}</span>
        </div>
      </div>

      <div className="game-scorebar">
        <div className={`game-scorecard ${game.turnUserId === host?.userId ? "game-scorecard--active" : ""}`}>
          <div className="game-scorecard__avatar">
            {host?.avatarUrl ? <img src={host.avatarUrl} alt={cleanName(host.displayName)} /> : <span>{playerInitials(host)}</span>}
          </div>
          <div className="game-scorecard__meta">
            <span className="game-scorecard__role">Anfitrião</span>
            <strong>{cleanName(host?.displayName ?? "Anfitrião")}</strong>
            <small>{game.turnUserId === host?.userId ? "Jogando agora" : "Aguardando"}</small>
          </div>
        </div>

        <div className="game-scorebar__center">
          <span className="game-scorebar__title">Mesa de sinuca</span>
          <strong>{game.shotSequence === 0 ? "Break inicial" : `Tacada ${game.shotSequence}`}</strong>
          <small>{game.tableType === "casual" ? "Partida amistosa" : `Valendo ${game.stakeChips ?? 0}`}</small>
        </div>

        <div className={`game-scorecard ${game.turnUserId === guest?.userId ? "game-scorecard--active" : ""}`}>
          <div className="game-scorecard__avatar">
            {guest?.avatarUrl ? <img src={guest.avatarUrl} alt={cleanName(guest.displayName)} /> : <span>{playerInitials(guest)}</span>}
          </div>
          <div className="game-scorecard__meta">
            <span className="game-scorecard__role">Adversário</span>
            <strong>{cleanName(guest?.displayName ?? "Aguardando")}</strong>
            <small>{game.turnUserId === guest?.userId ? "Jogando agora" : (guest ? "Aguardando" : "Entrando na mesa")}</small>
          </div>
        </div>
      </div>

      <div className="game-board-card">
        <div className="game-board-card__frame">
          <div
            ref={tableRef}
            className={`pool-table pool-table--reboot ${canShoot ? "pool-table--interactive" : ""}`}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerLeave={handlePointerUp}
          >
            <div className="pool-table__felt" />
            <div className="pool-table__marks" />
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
                  className="pool-ghost-dot"
                  style={{
                    left: `${aimGuide.ghostX}px`,
                    top: `${aimGuide.ghostY}px`,
                  }}
                />
                <div
                  className="pool-cue"
                  style={{
                    left: `${aimGuide.left}px`,
                    top: `${aimGuide.top}px`,
                    transform: `translate(-100%, -50%) rotate(${aimGuide.angle}rad) translateX(${-70 - power * 70}px)`,
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

      <div className="game-actionbar">
        <div className="game-power-card">
          <div className="game-power-card__labels">
            <span>Força</span>
            <strong>{Math.round(power * 100)}%</strong>
          </div>
          <input
            className="game-power-card__slider"
            type="range"
            min="18"
            max="100"
            value={Math.round(power * 100)}
            onChange={(event) => setPower(Number(event.target.value) / 100)}
            disabled={!canShoot}
          />
        </div>

        <div className="game-shot-hint">
          <strong>{canShoot ? "Arraste na mesa para mirar" : statusText}</strong>
          <small>Solte a mira e aperte o botão quando alinhar a tacada.</small>
        </div>

        <button className="primary-button game-shoot-button" type="button" disabled={!canShoot || shootBusy} onClick={handleShoot}>
          {shootBusy ? "Tacando..." : "Tacar"}
        </button>
      </div>
    </section>
  );
}
