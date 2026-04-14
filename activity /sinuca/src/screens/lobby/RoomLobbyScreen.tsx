import type { RoomPlayer, RoomSnapshot } from "../../types/activity";
import { cleanPlayerName, resolvePlayerAvatar } from "../../utils/roomPresentation";

type AuthState = "checking" | "ready" | "needs_consent";
type ConnectionState = "connecting" | "connected" | "offline";

type RoomLobbyScreenProps = {
  room: RoomSnapshot;
  isRoomHost: boolean;
  roomExitBusy: boolean;
  canHostStart: boolean;
  roomTopStatus: string;
  roomHostPlayer: RoomPlayer | null;
  roomOpponentPlayer: RoomPlayer | null;
  currentPlayer: RoomPlayer | null;
  gameStartBusy: boolean;
  authState: AuthState;
  resolvedUser: boolean;
  errorMessage: string | null;
  connectionState: ConnectionState;
  onExit: () => void;
  onStartGame: () => void;
  onToggleReady: () => void;
};

export default function RoomLobbyScreen({
  room,
  isRoomHost,
  roomExitBusy,
  canHostStart,
  roomTopStatus,
  roomHostPlayer,
  roomOpponentPlayer,
  currentPlayer,
  gameStartBusy,
  authState,
  resolvedUser,
  errorMessage,
  connectionState,
  onExit,
  onStartGame,
  onToggleReady,
}: RoomLobbyScreenProps) {
  const hostPlayer = roomHostPlayer ?? room.players[0] ?? null;

  return (
    <section className="lobby-panel lobby-panel--compact lobby-panel--room-stage lobby-panel--compact-stage">
      <div className="list-topbar list-topbar--room-stage list-topbar--room-stage-compact list-topbar--compact-room-stage">
        <button className="chip-button chip-button--back" type="button" disabled={roomExitBusy} onClick={onExit}>
          {roomExitBusy ? (isRoomHost ? "Fechando..." : "Saindo...") : (isRoomHost ? "Fechar sala" : "Sair")}
        </button>
        <div className="room-stage__top-meta room-stage__top-meta--compact-stage">
          <span className="room-stage__top-chip">{room.players.length}/2</span>
          <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>{roomTopStatus}</span>
        </div>
      </div>

      <div className="room-stage room-stage--final room-stage--compact room-stage--single room-stage--compact-stage">
        <div className="create-preview-card room-stage__preview room-stage__preview--compact room-stage__preview--compact-stage">
          <div className="compact-stage-head compact-stage-head--room">
            <div>
              <span className="compact-stage-head__eyebrow">Sala atual</span>
              <strong className="compact-stage-head__title">Preparar partida</strong>
            </div>
            <div className="compact-stage-head__chips compact-stage-head__chips--room">
              <span className="room-inline-chip">{room.players.length}/2</span>
              <span className="room-inline-chip room-inline-chip--status">{roomOpponentPlayer ? "fechada" : "vaga aberta"}</span>
            </div>
          </div>

          <div className="create-preview-shell create-preview-shell--room room-stage__players room-stage__players--compact-stage">
            {hostPlayer ? (
              <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-host participant-slot--compact-stage">
                <div className="participant-slot__avatar-wrap">
                  <img className="participant-slot__avatar" src={resolvePlayerAvatar(hostPlayer)} alt={room.hostDisplayName} />
                </div>
                <div className="participant-slot__copy">
                  <span className="participant-slot__name">{cleanPlayerName(hostPlayer)}</span>
                  <small className="participant-slot__role">anfitrião</small>
                </div>
                <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>
                  {canHostStart ? "pode iniciar" : roomOpponentPlayer ? "aguardando" : "vaga aberta"}
                </span>
              </div>
            ) : null}

            {roomOpponentPlayer ? (
              <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-opponent participant-slot--compact-stage">
                <div className="participant-slot__avatar-wrap">
                  <img className="participant-slot__avatar" src={resolvePlayerAvatar(roomOpponentPlayer)} alt={roomOpponentPlayer.displayName} />
                </div>
                <div className="participant-slot__copy">
                  <span className="participant-slot__name">{cleanPlayerName(roomOpponentPlayer)}</span>
                  <small className="participant-slot__role">adversário</small>
                </div>
                <span className={`room-ready-badge ${roomOpponentPlayer.ready ? "room-ready-badge--ready" : ""}`}>
                  {roomOpponentPlayer.ready ? "pronto" : "aguardando"}
                </span>
              </div>
            ) : (
              <div className="participant-slot participant-slot--ghost participant-slot--room-main participant-slot--room-opponent participant-slot--room-open participant-slot--compact-stage participant-slot--compact-open">
                <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                  <div className="participant-slot__unknown">?</div>
                </div>
                <div className="participant-slot__copy">
                  <span className="participant-slot__name">Aguardando adversário</span>
                  <small className="participant-slot__role">vaga aberta</small>
                </div>
                <span className="room-ready-badge">aguardando</span>
              </div>
            )}
          </div>

          <div className="room-stage__footer room-stage__footer--tight room-stage__footer--compact-stage">
            <div className={`room-stage__actions room-stage__actions--single ${isRoomHost ? "room-stage__actions--host" : "room-stage__actions--guest"}`}>
              {isRoomHost ? (
                <button
                  className={`primary-button room-stage__ready ${!canHostStart ? "primary-button--muted" : ""}`}
                  type="button"
                  disabled={!canHostStart || gameStartBusy}
                  onClick={onStartGame}
                >
                  {gameStartBusy ? "Abrindo mesa..." : "Iniciar partida"}
                </button>
              ) : (
                <button
                  className={`primary-button room-stage__ready ${currentPlayer?.ready ? "primary-button--muted" : ""}`}
                  type="button"
                  onClick={onToggleReady}
                >
                  {currentPlayer?.ready ? "Cancelar pronto" : "Marcar pronto"}
                </button>
              )}
            </div>
          </div>

          {authState === "needs_consent" && !resolvedUser ? (
            <p className="plain-copy">Autorize sua conta para usar fichas, criar mesa e entrar em partida.</p>
          ) : null}
          {errorMessage && connectionState !== "offline" ? <p className="error-copy">{errorMessage}</p> : null}
        </div>
      </div>
    </section>
  );
}
