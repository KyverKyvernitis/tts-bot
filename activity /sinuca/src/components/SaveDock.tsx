interface SaveDockProps {
  changedCount: number;
  sectionLabel: string;
  saving: boolean;
  onDiscard(): void;
  onSave(): void;
}

export function SaveDock({ changedCount, sectionLabel, saving, onDiscard, onSave }: SaveDockProps) {
  if (changedCount <= 0) return null;
  return (
    <div className="osk-save-dock" role="region" aria-label="Alterações pendentes">
      <div className="osk-save-dock-text">
        <strong>
          {changedCount} alteração{changedCount === 1 ? "" : "es"} pendente
          {changedCount === 1 ? "" : "s"}
        </strong>
        <small>em {sectionLabel}</small>
      </div>
      <div className="osk-save-dock-actions">
        <button className="osk-btn osk-btn--ghost osk-btn--sm" onClick={onDiscard} disabled={saving}>
          Descartar
        </button>
        <button className="osk-btn osk-btn--primary osk-btn--sm" onClick={onSave} disabled={saving}>
          {saving ? "Salvando..." : "Salvar"}
        </button>
      </div>
    </div>
  );
}
