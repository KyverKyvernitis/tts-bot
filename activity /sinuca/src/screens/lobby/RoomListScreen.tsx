import type { RoomSnapshot } from "../../types/activity";
import { cleanPlayerName, formatRoomCount, resolvePlayerAvatar } from "../../utils/roomPresentation";

type RoomListScreenProps = {
  rooms: RoomSnapshot[];
  authBusy: boolean;
  resolvedUser: boolean;
  onBack: () => void;
  onAuthorize: () => void;
  onJoinRoom: (roomId: string) => void;
  formatStakeOptionLabel: (stake: number) => string;
  formatStatusLabel: (room: RoomSnapshot) => string;
};

export default function RoomListScreen({
  rooms,
  authBusy,
  resolvedUser,
  onBack,
  onAuthorize,
  onJoinRoom,
  formatStakeOptionLabel,
  formatStatusLabel,
}: RoomListScreenProps) {
  return (
    <section className="lobby-panel lobby-panel--compact lobby-panel--list">
      <div className="list-topbar">
        <button className="chip-button chip-button--back" type="button" onClick={onBack}>Voltar</button>
        <div className="list-topbar__count">{formatRoomCount(rooms.length)}</div>
      </div>

      <div className="room-list-stack room-list-stack--immersive">
        {rooms.length === 0 ? (
          <div className="empty-card empty-card--soft empty-card--home empty-card--list">
            <strong>Nenhuma mesa aberta</strong>
            <span>Crie uma para começar.</span>
          </div>
        ) : (
          rooms.map((entry) => {
            const host = entry.players.find((player) => player.userId === entry.hostUserId) ?? entry.players[0];
            const opponent = entry.players.find((player) => player.userId !== entry.hostUserId) ?? null;

            return (
              <article key={entry.roomId} className="room-entry-card room-entry-card--soft room-entry-card--showdown">
                <div className="room-entry-card__showdown">
                  <div className="participant-slot participant-slot--filled participant-slot--list-card">
                    <div className="participant-slot__avatar-wrap">
                      <img className="participant-slot__avatar" src={resolvePlayerAvatar(host)} alt={host.displayName} />
                    </div>
                    <span className="participant-slot__name">{cleanPlayerName(host)}</span>
                    <small className="participant-slot__role">anfitrião</small>
                  </div>

                  <div className="participant-slot__versus participant-slot__versus--list">vs.</div>

                  {opponent ? (
                    <div className="participant-slot participant-slot--filled participant-slot--list-card">
                      <div className="participant-slot__avatar-wrap">
                        <img className="participant-slot__avatar" src={resolvePlayerAvatar(opponent)} alt={opponent.displayName} />
                      </div>
                      <span className="participant-slot__name">{cleanPlayerName(opponent)}</span>
                      <small className="participant-slot__role">adversário</small>
                    </div>
                  ) : (
                    <div className="participant-slot participant-slot--ghost participant-slot--list-card">
                      <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                        <div className="participant-slot__unknown">?</div>
                      </div>
                      <span className="participant-slot__name">Aguardando jogador</span>
                      <small className="participant-slot__role">vaga aberta</small>
                    </div>
                  )}
                </div>

                <div className="room-entry-card__footer room-entry-card__footer--compact">
                  <div className="room-entry-card__meta room-entry-card__meta--chips">
                    <span className="room-inline-chip">{entry.players.length}/2</span>
                    <span className="room-inline-chip">{formatStakeOptionLabel(entry.tableType === "stake" ? (entry.stakeChips ?? 0) : 0)}</span>
                    <span className={`status-badge status-badge--${entry.status} room-inline-chip room-inline-chip--status`}>
                      {formatStatusLabel(entry)}
                    </span>
                  </div>

                  <button
                    className="primary-button room-entry-card__join"
                    type="button"
                    disabled={authBusy || entry.players.length >= 2}
                    onClick={() => {
                      if (!resolvedUser) {
                        onAuthorize();
                        return;
                      }
                      onJoinRoom(entry.roomId);
                    }}
                  >
                    {!resolvedUser ? (authBusy ? "Autorizando..." : "Autorizar") : entry.players.length >= 2 ? "Mesa cheia" : "Entrar"}
                  </button>
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}
