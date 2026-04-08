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

export type ShotPipelineDebugEvent = {
  stage: string;
  roomId?: string | null;
  angle?: number | null;
  power?: number | null;
  cueX?: number | null;
  cueY?: number | null;
  reason?: string | null;
  note?: string | null;
};

type ShotPipelineDebugState = {
  lastStage: string;
  lastStageAt: number | null;
  lastBlockReason: string | null;
  lastTransport: string | null;
  wsAttempted: boolean;
  wsDelivered: boolean | null;
  httpFallbackAttempted: boolean;
  httpPrimaryAttempted: boolean;
  debugPingCount: number;
  lastPingStage: string | null;
  lastPingStatus: string | null;
  roomId: string | null;
  gameId: string | null;
  shotSequence: number | null;
  gameStatus: string | null;
  ballInHandUserId: string | null;
  angle: number | null;
  power: number | null;
  cueX: number | null;
  cueY: number | null;
  note: string | null;
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
  shotPipelineDebug: ShotPipelineDebugState;
  onShotDebugEvent: (event: ShotPipelineDebugEvent) => void;
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
  shotPipelineDebug,
  onShotDebugEvent,
  onAimStateChange,
  onExit,
  onShoot,
  onForceReturnToLobby,
}: GameScreenProps) {
  if (game) {
    return (
      <>
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
        onShotDebugEvent={onShotDebugEvent}
      />
      <section className="debug-card">
        <h3 className="debug-card__title">Debug da tacada</h3>
        <div className="debug-card__grid">
          <div><strong>Última etapa</strong><span>{shotPipelineDebug.lastStage}</span></div>
          <div><strong>Hora</strong><span>{shotPipelineDebug.lastStageAt ? new Date(shotPipelineDebug.lastStageAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"}</span></div>
          <div><strong>Bloqueio</strong><span>{shotPipelineDebug.lastBlockReason ?? "nenhum"}</span></div>
          <div><strong>Transporte</strong><span>{shotPipelineDebug.lastTransport ?? "nenhum"}</span></div>
          <div><strong>WS tentou</strong><span>{shotPipelineDebug.wsAttempted ? "sim" : "não"}</span></div>
          <div><strong>WS entregou</strong><span>{shotPipelineDebug.wsDelivered === null ? "—" : (shotPipelineDebug.wsDelivered ? "sim" : "não")}</span></div>
          <div><strong>HTTP fallback</strong><span>{shotPipelineDebug.httpFallbackAttempted ? "sim" : "não"}</span></div>
          <div><strong>HTTP primário</strong><span>{shotPipelineDebug.httpPrimaryAttempted ? "sim" : "não"}</span></div>
          <div><strong>Ping etapa</strong><span>{shotPipelineDebug.lastPingStage ?? "nenhum"}</span></div>
          <div><strong>Ping status</strong><span>{shotPipelineDebug.lastPingStatus ?? "nenhum"}</span></div>
          <div><strong>Qtd. pings</strong><span>{shotPipelineDebug.debugPingCount}</span></div>
          <div><strong>Room</strong><span>{shotPipelineDebug.roomId ?? "—"}</span></div>
          <div><strong>Game</strong><span>{shotPipelineDebug.gameId ?? "—"}</span></div>
          <div><strong>Shot seq</strong><span>{shotPipelineDebug.shotSequence ?? "—"}</span></div>
          <div><strong>Status</strong><span>{shotPipelineDebug.gameStatus ?? "—"}</span></div>
          <div><strong>Ball in hand</strong><span>{shotPipelineDebug.ballInHandUserId ?? "—"}</span></div>
          <div><strong>Ângulo</strong><span>{shotPipelineDebug.angle ?? "—"}</span></div>
          <div><strong>Power</strong><span>{shotPipelineDebug.power ?? "—"}</span></div>
          <div><strong>Cue X</strong><span>{shotPipelineDebug.cueX ?? "—"}</span></div>
          <div><strong>Cue Y</strong><span>{shotPipelineDebug.cueY ?? "—"}</span></div>
          <div style={{ gridColumn: "1 / -1" }}><strong>Nota</strong><span>{shotPipelineDebug.note ?? "sem nota"}</span></div>
        </div>
      </section>
      </>
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
