import { useState } from "react";
import type { CSSProperties } from "react";
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
  const [debugExpanded, setDebugExpanded] = useState(false);

  if (game) {
    const stageTime = shotPipelineDebug.lastStageAt
      ? new Date(shotPipelineDebug.lastStageAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : "—";

    const floatingDebugWrapStyle: CSSProperties = {
      position: "fixed",
      right: 14,
      bottom: 14,
      zIndex: 20,
      display: "flex",
      flexDirection: "column",
      alignItems: "flex-end",
      gap: 8,
      pointerEvents: "none",
      maxWidth: "min(320px, calc(100vw - 28px))",
    };

    const floatingDebugButtonStyle: CSSProperties = {
      pointerEvents: "auto",
      border: "1px solid rgba(255,255,255,0.14)",
      borderRadius: 999,
      background: "rgba(6, 12, 20, 0.82)",
      color: "rgba(255,255,255,0.96)",
      padding: "10px 14px",
      fontSize: 12,
      fontWeight: 700,
      letterSpacing: "0.04em",
      backdropFilter: "blur(10px)",
      WebkitBackdropFilter: "blur(10px)",
      boxShadow: "0 10px 28px rgba(0, 0, 0, 0.28)",
      touchAction: "manipulation",
    };

    const floatingDebugCardStyle: CSSProperties = {
      pointerEvents: "auto",
      width: "min(320px, calc(100vw - 28px))",
      maxHeight: "min(46vh, 360px)",
      overflowY: "auto",
      border: "1px solid rgba(255,255,255,0.12)",
      borderRadius: 18,
      background: "rgba(6, 12, 20, 0.82)",
      color: "rgba(255,255,255,0.92)",
      padding: 12,
      backdropFilter: "blur(10px)",
      WebkitBackdropFilter: "blur(10px)",
      boxShadow: "0 16px 34px rgba(0, 0, 0, 0.32)",
      overscrollBehavior: "contain",
    };

    const floatingDebugGridStyle: CSSProperties = {
      display: "grid",
      gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
      gap: "8px 12px",
      fontSize: 12,
    };

    const fullRowStyle: CSSProperties = { gridColumn: "1 / -1" };

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
        <section aria-label="Debug da tacada" style={floatingDebugWrapStyle}>
          {debugExpanded ? (
            <div style={floatingDebugCardStyle}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, marginBottom: 10 }}>
                <h3 className="debug-card__title" style={{ margin: 0 }}>Debug da tacada</h3>
                <button
                  type="button"
                  aria-label="Minimizar debug da tacada"
                  onClick={() => setDebugExpanded(false)}
                  style={{
                    pointerEvents: "auto",
                    border: "1px solid rgba(255,255,255,0.12)",
                    borderRadius: 999,
                    background: "rgba(255,255,255,0.04)",
                    color: "rgba(255,255,255,0.92)",
                    width: 28,
                    height: 28,
                    fontSize: 16,
                    lineHeight: 1,
                    touchAction: "manipulation",
                  }}
                >
                  ×
                </button>
              </div>
              <div className="debug-card__grid" style={floatingDebugGridStyle}>
                <div><strong>Última etapa</strong><span>{shotPipelineDebug.lastStage}</span></div>
                <div><strong>Hora</strong><span>{stageTime}</span></div>
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
                <div style={fullRowStyle}><strong>Nota</strong><span>{shotPipelineDebug.note ?? "sem nota"}</span></div>
              </div>
            </div>
          ) : null}
          <button
            type="button"
            aria-expanded={debugExpanded}
            aria-label={debugExpanded ? "Debug da tacada expandido" : "Abrir debug da tacada"}
            onClick={() => setDebugExpanded((value) => !value)}
            style={floatingDebugButtonStyle}
          >
            {debugExpanded ? "Ocultar debug" : `Debug · ${shotPipelineDebug.lastStage}`}
          </button>
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
