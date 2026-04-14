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
    <section className="lobby-panel lobby-panel--compact lobby-panel--room-stage">
      <div className="list-topbar list-topbar--room-stage list-topbar--room-stage-compact">
        <button className="chip-button chip-button--back" type="button" disabled={roomExitBusy} onClick={onExit}>
          {roomExitBusy ? (isRoomHost ? "Fechando..." : "Saindo...") : (isRoomHost ? "Fechar sala" : "Sair")}
        </button>
        <div className="room-stage__top-meta">
          <span className="room-stage__top-chip">{room.players.length}/2</span>
          <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>{roomTopStatus}</span>
        </div>
      </div>

      <div className="room-stage room-stage--final room-stage--compact room-stage--single">
        <div className="create-preview-card room-stage__preview room-stage__preview--compact">
          <div className="create-preview-shell create-preview-shell--room room-stage__players">
            {hostPlayer ? (
              <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-host">
                <div className="participant-slot__avatar-wrap">
                  <img className="participant-slot__avatar" src={resolvePlayerAvatar(hostPlayer)} alt={room.hostDisplayName} />
                </div>
                <span className="participant-slot__name">{cleanPlayerName(hostPlayer)}</span>
                <small className="participant-slot__role">anfitrião</small>
                <span className={`room-ready-badge ${canHostStart ? "room-ready-badge--ready" : ""}`}>
                  {canHostStart ? "pode iniciar" : roomOpponentPlayer ? "aguardando" : "vaga aberta"}
                </span>
              </div>
            ) : null}

            {roomOpponentPlayer ? (
              <div className="participant-slot participant-slot--filled participant-slot--room-main participant-slot--room-opponent">
                <div className="participant-slot__avatar-wrap">
                  <img className="participant-slot__avatar" src={resolvePlayerAvatar(roomOpponentPlayer)} alt={roomOpponentPlayer.displayName} />
                </div>
                <span className="participant-slot__name">{cleanPlayerName(roomOpponentPlayer)}</span>
                <small className="participant-slot__role">adversário</small>
                <span className={`room-ready-badge ${roomOpponentPlayer.ready ? "room-ready-badge--ready" : ""}`}>
                  {roomOpponentPlayer.ready ? "pronto" : "aguardando"}
                </span>
              </div>
            ) : (
              <div className="participant-slot participant-slot--ghost participant-slot--room-main participant-slot--room-opponent participant-slot--room-open">
                <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                  <div className="participant-slot__unknown">?</div>
                </div>
                <span className="participant-slot__name">Aguardando adversário</span>
                <small className="participant-slot__role">vaga aberta</small>
                <span className="room-ready-badge">aguardando</span>
              </div>
            )}
          </div>

          <div className="room-stage__footer room-stage__footer--tight">
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
