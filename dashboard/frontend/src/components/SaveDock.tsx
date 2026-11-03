import { LoaderCircle, RotateCcw, Save } from "lucide-react";

interface SaveDockProps {
  changedCount: number;
  sectionLabel: string;
  saving: boolean;
  onDiscard(): void;
  onSave(): void;
}

export function SaveDock({ changedCount, sectionLabel, saving, onDiscard, onSave }: SaveDockProps) {
  if (changedCount <= 0) return null;
  return <div className="osk-save-dock" role="region" aria-label="Alterações não salvas" aria-live="polite" aria-busy={saving}>
    <div className="osk-save-dock-copy">
      <span className="osk-save-dot" aria-hidden="true" />
      <span><strong>{changedCount} campo{changedCount === 1 ? " alterado" : "s alterados"}</strong><small title={sectionLabel}>{sectionLabel}</small></span>
    </div>
    <div className="osk-save-dock-actions">
      <button type="button" className="osk-secondary-button osk-secondary-button--small" onClick={onDiscard} disabled={saving}><RotateCcw size={15} aria-hidden="true" />Descartar</button>
      <button type="button" className="osk-primary-button osk-primary-button--small" onClick={onSave} disabled={saving}>{saving ? <LoaderCircle size={15} className="osk-workspace-saving" aria-hidden="true" /> : <Save size={15} aria-hidden="true" />}{saving ? "Salvando…" : "Salvar no servidor"}</button>
    </div>
  </div>;
}
