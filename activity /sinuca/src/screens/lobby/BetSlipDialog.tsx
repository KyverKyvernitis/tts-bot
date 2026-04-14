import type { ChangeEvent } from "react";
import type { RoomSnapshot } from "../../types/activity";
import { cleanPlayerName, resolvePlayerAvatar } from "../../utils/roomPresentation";

type BetSlipDialogProps = {
  room: RoomSnapshot | null;
  amount: number;
  selectedUserId: string | null;
  onSelectPlayer: (userId: string) => void;
  onAmountChange: (amount: number) => void;
  onClose: () => void;
  onConfirm: () => void;
};

const QUICK_AMOUNTS = [25, 50, 100, 250];

export default function BetSlipDialog({
  room,
  amount,
  selectedUserId,
  onSelectPlayer,
  onAmountChange,
  onClose,
  onConfirm,
}: BetSlipDialogProps) {
  if (!room) return null;

  const players = room.players.slice(0, 2);
  const canConfirm = Boolean(selectedUserId) && amount > 0 && players.length === 2;

  const handleAmountInput = (event: ChangeEvent<HTMLInputElement>) => {
    const nextAmount = Number(event.target.value);
    if (!Number.isFinite(nextAmount)) {
      onAmountChange(0);
      return;
    }
    onAmountChange(Math.max(0, Math.floor(nextAmount)));
  };

  return (
    <div className="activity-confirm activity-bet" role="dialog" aria-modal="true" aria-live="polite">
      <div className="activity-confirm__backdrop" onClick={onClose} />
      <div className="activity-confirm__panel activity-confirm__panel--bet">
        <div className="activity-confirm__panel-bg" aria-hidden="true" />
        <div className="activity-confirm__content activity-bet__content">
          <div className="activity-confirm__title activity-bet__title">Bet na partida</div>
          <div className="activity-confirm__body activity-bet__body">
            Escolha o jogador, defina o valor e confirme. O modo espectador entra na próxima etapa da activity.
          </div>

          <div className="activity-bet__players">
            {players.map((player) => {
              const selected = selectedUserId === player.userId;
              return (
                <button
                  key={player.userId}
                  type="button"
                  className={`activity-bet__player ${selected ? "activity-bet__player--selected" : ""}`}
                  onClick={() => onSelectPlayer(player.userId)}
                >
                  <span className="activity-bet__player-avatar-wrap">
                    <img
                      className="activity-bet__player-avatar"
                      src={resolvePlayerAvatar(player)}
                      alt={player.displayName}
                    />
                  </span>
                  <span className="activity-bet__player-copy">
                    <strong>{cleanPlayerName(player)}</strong>
                    <small>{player.userId === room.hostUserId ? "anfitrião" : "adversário"}</small>
                  </span>
                </button>
              );
            })}
          </div>

          <div className="activity-bet__amount-block">
            <label className="activity-bet__label" htmlFor="bet-amount-input">Valor da aposta</label>
            <div className="activity-bet__amount-row">
              <input
                id="bet-amount-input"
                className="activity-bet__amount-input"
                type="number"
                min={1}
                step={1}
                value={amount}
                onChange={handleAmountInput}
              />
              <span className="activity-bet__amount-suffix">fichas</span>
            </div>
            <div className="activity-bet__quick-row">
              {QUICK_AMOUNTS.map((quickAmount) => (
                <button
                  key={quickAmount}
                  type="button"
                  className={`activity-bet__quick-chip ${amount === quickAmount ? "activity-bet__quick-chip--active" : ""}`}
                  onClick={() => onAmountChange(quickAmount)}
                >
                  {quickAmount}
                </button>
              ))}
            </div>
          </div>

          <div className="activity-confirm__actions activity-bet__actions">
            <button type="button" className="activity-confirm__button activity-confirm__button--ghost" onClick={onClose}>
              Cancelar
            </button>
            <button
              type="button"
              className="activity-confirm__button activity-confirm__button--danger"
              disabled={!canConfirm}
              onClick={onConfirm}
            >
              Confirmar Bet
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
