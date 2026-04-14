import { useMemo, useState } from "react";
import type { RoomSnapshot } from "../../types/activity";
import { cleanPlayerName, formatRoomCount, resolvePlayerAvatar } from "../../utils/roomPresentation";
import BetSlipDialog from "./BetSlipDialog";

type RoomListScreenProps = {
  rooms: RoomSnapshot[];
  authBusy: boolean;
  resolvedUser: boolean;
  onBack: () => void;
  onAuthorize: () => void;
  onJoinRoom: (roomId: string) => void;
  onBetPlaceholder: (room: RoomSnapshot, targetUserId: string, amount: number) => void;
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
  onBetPlaceholder,
  formatStakeOptionLabel,
  formatStatusLabel,
}: RoomListScreenProps) {
  const [betRoomId, setBetRoomId] = useState<string | null>(null);
  const [betTargetUserId, setBetTargetUserId] = useState<string | null>(null);
  const [betAmount, setBetAmount] = useState<number>(25);

  const roomsById = useMemo(() => new Map(rooms.map((entry) => [entry.roomId, entry])), [rooms]);
  const activeBetRoom = betRoomId ? roomsById.get(betRoomId) ?? null : null;

  const closeBetDialog = () => {
    setBetRoomId(null);
    setBetTargetUserId(null);
    setBetAmount(25);
  };

  const openBetDialog = (room: RoomSnapshot) => {
    const bettablePlayers = room.players.slice(0, 2);
    setBetRoomId(room.roomId);
    setBetTargetUserId(bettablePlayers[0]?.userId ?? null);
    setBetAmount(room.tableType === "stake" ? Math.max(room.stakeChips ?? 25, 25) : 25);
  };

  const confirmBetDialog = () => {
    if (!activeBetRoom || !betTargetUserId || betAmount <= 0) return;
    onBetPlaceholder(activeBetRoom, betTargetUserId, betAmount);
    closeBetDialog();
  };

  return (
    <section className="lobby-panel lobby-panel--compact lobby-panel--list lobby-panel--list-compact">
      <BetSlipDialog
        room={activeBetRoom}
        amount={betAmount}
        selectedUserId={betTargetUserId}
        onSelectPlayer={setBetTargetUserId}
        onAmountChange={setBetAmount}
        onClose={closeBetDialog}
        onConfirm={confirmBetDialog}
      />

      <div className="list-topbar list-topbar--list-compact">
        <button className="chip-button chip-button--back" type="button" onClick={onBack}>Voltar</button>
        <div className="list-topbar__count">{formatRoomCount(rooms.length)}</div>
      </div>

      <div className="room-list-stack room-list-stack--immersive room-list-stack--compact-view">
        {rooms.length === 0 ? (
          <div className="empty-card empty-card--soft empty-card--home empty-card--list empty-card--compact-list">
            <strong>Nenhuma mesa aberta</strong>
            <span>Crie uma para começar.</span>
          </div>
        ) : (
          rooms.map((entry) => {
            const host = entry.players.find((player) => player.userId === entry.hostUserId) ?? entry.players[0];
            const opponent = entry.players.find((player) => player.userId !== entry.hostUserId) ?? null;
            const isLive = entry.status === "in_game" && Boolean(opponent);
            const actionLabel = !resolvedUser
              ? (authBusy ? "Autorizando..." : "Autorizar")
              : isLive
                ? "Bet"
                : entry.players.length >= 2
                  ? "Mesa cheia"
                  : "Entrar";
            const actionDisabled = authBusy || (!isLive && entry.players.length >= 2);

            return (
              <article
                key={entry.roomId}
                className={`room-entry-card room-entry-card--soft room-entry-card--compact-view ${isLive ? "room-entry-card--live" : ""}`}
              >
                <div className="room-entry-card__compact-grid">
                  <div className="room-entry-card__showdown room-entry-card__showdown--compact-view">
                    <div className="participant-slot participant-slot--filled participant-slot--list-card participant-slot--list-inline">
                      <div className="participant-slot__avatar-wrap">
                        <img className="participant-slot__avatar" src={resolvePlayerAvatar(host)} alt={host.displayName} />
                      </div>
                      <span className="participant-slot__name">{cleanPlayerName(host)}</span>
                      <small className="participant-slot__role">anfitrião</small>
                    </div>

                    <div className={`participant-slot__versus participant-slot__versus--list ${isLive ? "participant-slot__versus--live" : ""}`}>
                      {isLive ? "ao vivo" : "vs."}
                    </div>

                    {opponent ? (
                      <div className="participant-slot participant-slot--filled participant-slot--list-card participant-slot--list-inline">
                        <div className="participant-slot__avatar-wrap">
                          <img className="participant-slot__avatar" src={resolvePlayerAvatar(opponent)} alt={opponent.displayName} />
                        </div>
                        <span className="participant-slot__name">{cleanPlayerName(opponent)}</span>
                        <small className="participant-slot__role">adversário</small>
                      </div>
                    ) : (
                      <div className="participant-slot participant-slot--ghost participant-slot--list-card participant-slot--list-inline participant-slot--list-inline-open">
                        <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                          <div className="participant-slot__unknown">?</div>
                        </div>
                        <span className="participant-slot__name">Aguardando jogador</span>
                        <small className="participant-slot__role">vaga aberta</small>
                      </div>
                    )}
                  </div>

                  <div className="room-entry-card__footer room-entry-card__footer--compact room-entry-card__footer--compact-view">
                    <div className="room-entry-card__meta room-entry-card__meta--chips room-entry-card__meta--compact-cluster">
                      <span className="room-inline-chip">{entry.players.length}/2</span>
                      <span className="room-inline-chip">{formatStakeOptionLabel(entry.tableType === "stake" ? (entry.stakeChips ?? 0) : 0)}</span>
                      <span className={`status-badge status-badge--${entry.status} room-inline-chip room-inline-chip--status`}>
                        {formatStatusLabel(entry)}
                      </span>
                    </div>

                    <button
                      className={`primary-button room-entry-card__join room-entry-card__join--compact ${isLive ? "room-entry-card__join--bet" : ""}`}
                      type="button"
                      disabled={actionDisabled}
                      onClick={() => {
                        if (!resolvedUser) {
                          onAuthorize();
                          return;
                        }
                        if (isLive) {
                          openBetDialog(entry);
                          return;
                        }
                        onJoinRoom(entry.roomId);
                      }}
                    >
                      {actionLabel}
                    </button>
                  </div>
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}
