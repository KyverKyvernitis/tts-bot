import type { AimPointerMode, AimStateSnapshot, GameSnapshot, RoomSnapshot } from "../types/activity";
import GameStage from "../games/GameStage";

type ShotInput = {
  angle: number;
  power: number;
  cueX?: number | null;
  cueY?: number | null;
  calledPocket?: number | null;
  spinX?: number | null;
  spinY?: number | null;
};

type AimUpdate = {
  visible: boolean;
  angle: number;
  cueX?: number | null;
  cueY?: number | null;
  power?: number | null;
  seq?: number;
  mode: AimPointerMode;
};

type GameScreenProps = {
  room: RoomSnapshot;
  game: GameSnapshot | null;
  currentUserId: string;
  shootBusy: boolean;
  exitBusy: boolean;
  isRoomHost: boolean;
  opponentAim: AimStateSnapshot | null;
  gameLoadingTimedOut: boolean;
  loadingOverlayDebug: string;
  onAimStateChange: (aim: AimUpdate) => void;
  onExit: () => void;
  onShoot: (shot: ShotInput) => Promise<void>;
  onForceReturnToLobby: () => void;
};

export default function GameScreen({
  room,
  game,
  currentUserId,
  shootBusy,
  exitBusy,
  isRoomHost,
  opponentAim,
  gameLoadingTimedOut,
  loadingOverlayDebug,
  onAimStateChange,
  onExit,
  onShoot,
  onForceReturnToLobby,
}: GameScreenProps) {
  if (game) {
    return (
      <GameStage
        room={room}
        game={game}
        currentUserId={currentUserId}
        shootBusy={shootBusy}
        exitBusy={exitBusy}
        opponentAim={opponentAim}
        onAimStateChange={onAimStateChange}
        onExit={onExit}
        onShoot={onShoot}
      />
    );
  }

  return (
    <section className="lobby-panel lobby-panel--compact lobby-panel--room-stage">
      <div className="empty-card empty-card--soft empty-card--home empty-card--list">
        <strong>Carregando a mesa...</strong>
        <span>Sincronizando a partida para os dois lados.</span>
        {gameLoadingTimedOut ? (
          <p className="plain-copy" style={{ marginTop: 10 }}>
            A mesa demorou demais para sincronizar. Você pode voltar ao lobby e encerrar esta sala agora.
          </p>
        ) : null}
        {loadingOverlayDebug ? (
          <pre className="plain-copy" style={{ marginTop: 12, textAlign: "left", whiteSpace: "pre-wrap", maxWidth: 560, opacity: 0.78 }}>
            {loadingOverlayDebug}
          </pre>
        ) : null}
        <div style={{ display: "flex", justifyContent: "center", marginTop: 14 }}>
          <button
            className="primary-button"
            type="button"
            onClick={onForceReturnToLobby}
            disabled={exitBusy}
          >
            {exitBusy ? "Voltando..." : "Voltar ao lobby"}
          </button>
        </div>
      </div>
    </section>
  );
}
