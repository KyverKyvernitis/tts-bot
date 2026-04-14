import type { ActivityUser, RoomPlayer, RoomSnapshot } from "../../types/activity";
import { cleanPlayerName, resolvePlayerAvatar } from "../../utils/roomPresentation";

type CreateRoomScreenProps = {
  currentUser: ActivityUser;
  createPreviewHostPlayer: RoomPlayer | null;
  createPreviewOpponentPlayer: RoomPlayer | null;
  createPreviewRoom: RoomSnapshot | null;
  resolvedUser: boolean;
  authBusy: boolean;
  isServer: boolean;
  createStake: number;
  balanceLoaded: boolean;
  canAffordSelectedStake: boolean;
  onAuthorize: () => void;
  onOpenRoom: () => void;
  onClose: () => void;
};

export default function CreateRoomScreen({
  currentUser,
  createPreviewHostPlayer,
  createPreviewOpponentPlayer,
  createPreviewRoom,
  resolvedUser,
  authBusy,
  isServer,
  createStake,
  balanceLoaded,
  canAffordSelectedStake,
  onAuthorize,
  onOpenRoom,
  onClose,
}: CreateRoomScreenProps) {
  const previewHostAvatar = resolvePlayerAvatar(createPreviewHostPlayer ?? {
    userId: currentUser.userId,
    avatarUrl: currentUser.avatarUrl ?? null,
  });
  const previewHostName = cleanPlayerName({ displayName: createPreviewHostPlayer?.displayName ?? currentUser.displayName });

  return (
    <section className="lobby-panel lobby-panel--compact lobby-panel--create lobby-panel--compact-stage">
      <div className="list-topbar list-topbar--create list-topbar--compact-create list-topbar--single">
        <button className="chip-button chip-button--back" type="button" onClick={onClose}>Voltar</button>
      </div>

      <div className="create-layout create-layout--final">
        <div className="create-preview-card create-preview-card--final create-preview-card--single create-preview-card--compact-stage">
          <div className="compact-stage-head">
            <div>
              <span className="compact-stage-head__eyebrow">Mesa nova</span>
              <strong className="compact-stage-head__title">Prévia da sala</strong>
            </div>
            <div className="compact-stage-head__chips">
              <span className="room-inline-chip">1/2</span>
              <span className="room-inline-chip room-inline-chip--status">aguardando</span>
            </div>
          </div>

          <div className="create-preview-shell create-preview-shell--final create-preview-shell--create-compact create-preview-shell--compact-stage">
            <div className="participant-slot participant-slot--filled participant-slot--compact participant-slot--create-main participant-slot--compact-stage">
              <div className="participant-slot__avatar-wrap">
                <img className="participant-slot__avatar" src={previewHostAvatar} alt={createPreviewHostPlayer?.displayName ?? currentUser.displayName} />
              </div>
              <div className="participant-slot__copy">
                <span className="participant-slot__name">{previewHostName}</span>
                <small className="participant-slot__role">você</small>
              </div>
            </div>

            {createPreviewOpponentPlayer ? (
              <div className="participant-slot participant-slot--filled participant-slot--compact participant-slot--create-main participant-slot--compact-stage">
                <div className="participant-slot__avatar-wrap">
                  <img className="participant-slot__avatar" src={resolvePlayerAvatar(createPreviewOpponentPlayer)} alt={createPreviewOpponentPlayer.displayName} />
                </div>
                <div className="participant-slot__copy">
                  <span className="participant-slot__name">{cleanPlayerName(createPreviewOpponentPlayer)}</span>
                  <small className="participant-slot__role">jogador</small>
                </div>
              </div>
            ) : (
              <div className="participant-slot participant-slot--ghost participant-slot--compact participant-slot--create-main participant-slot--compact-stage participant-slot--compact-open">
                <div className="participant-slot__avatar-wrap participant-slot__avatar-wrap--ghost">
                  <div className="participant-slot__unknown">?</div>
                </div>
                <div className="participant-slot__copy">
                  <span className="participant-slot__name">Aguardando adversário</span>
                  <small className="participant-slot__role">vaga aberta</small>
                </div>
              </div>
            )}
          </div>

          <div className="create-preview-footer create-preview-footer--solo create-preview-footer--compact-stage">
            {!resolvedUser ? (
              <button className="primary-button create-submit create-submit--compact" type="button" disabled={authBusy} onClick={onAuthorize}>
                {authBusy ? "Autorizando..." : "Autorizar conta"}
              </button>
            ) : (
              <button
                className="primary-button create-submit create-submit--compact"
                type="button"
                disabled={!createPreviewRoom || (isServer && createStake > 0 && balanceLoaded && !canAffordSelectedStake)}
                onClick={onOpenRoom}
              >
                {createPreviewRoom ? "Abrir mesa" : "Abrindo mesa..."}
              </button>
            )}
          </div>

          {isServer && createStake > 0 && !balanceLoaded ? (
            <p className="plain-copy create-preview-note">Carregando fichas...</p>
          ) : null}
          {isServer && createStake > 0 && balanceLoaded && !canAffordSelectedStake ? (
            <p className="error-copy create-preview-note">Você não tem fichas suficientes para essa entrada.</p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
