type HomeLobbyScreenProps = {
  resolvedUser: boolean;
  authBusy: boolean;
  createRoomBusy: boolean;
  initReadyForServerActions: boolean;
  isServer: boolean;
  onAuthorize: () => void;
  onCreateRoom: () => void;
  onOpenRoomList: () => void;
};

export default function HomeLobbyScreen({
  resolvedUser,
  authBusy,
  createRoomBusy,
  initReadyForServerActions,
  isServer,
  onAuthorize,
  onCreateRoom,
  onOpenRoomList,
}: HomeLobbyScreenProps) {
  return (
    <section className="home-lobby home-lobby--landscape home-lobby--streamlined">
      {!resolvedUser ? (
        <div className="menu-buttons menu-buttons--single menu-buttons--compact menu-buttons--hero menu-buttons--hero-late">
          <button
            className="menu-button menu-button--authorize"
            type="button"
            disabled={authBusy}
            aria-busy={authBusy}
            onClick={onAuthorize}
          >
            <span className="menu-button__eyebrow">Conta Discord</span>
            <strong>{authBusy ? "Autorizando conta..." : "Autorizar conta"}</strong>
            <small>{authBusy ? "Confirme a janela de autorização do Discord." : "Autorize para criar mesa, entrar e usar fichas."}</small>
          </button>
        </div>
      ) : (
        <div className="menu-buttons menu-buttons--home menu-buttons--compact menu-buttons--hero menu-buttons--hero-late">
          <button
            className="menu-button menu-button--create"
            type="button"
            disabled={createRoomBusy || !initReadyForServerActions}
            onClick={onCreateRoom}
          >
            <span className="menu-button__eyebrow">Mesa nova</span>
            <strong>{createRoomBusy ? "Abrindo mesa..." : "Criar mesa"}</strong>
            <small>{createRoomBusy ? "Entrando na sala..." : (!initReadyForServerActions && isServer ? "Carregando fichas..." : "Abra uma mesa.")}</small>
          </button>

          <button
            className="menu-button menu-button--join"
            type="button"
            onClick={onOpenRoomList}
          >
            <span className="menu-button__eyebrow">Mesas abertas</span>
            <strong>Entrar</strong>
            <small>Veja as mesas abertas.</small>
          </button>
        </div>
      )}
    </section>
  );
}
