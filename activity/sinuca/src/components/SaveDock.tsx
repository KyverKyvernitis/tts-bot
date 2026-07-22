import { RotateCcw, Save } from "lucide-react";

interface SaveDockProps {
  changedCount: number;
  sectionLabel: string;
  saving: boolean;
  onDiscard(): void;
  onSave(): void;
}

export function SaveDock({ changedCount, sectionLabel, saving, onDiscard, onSave }: SaveDockProps) {
  if (changedCount <= 0) return null;
  return <div className="osk-save-dock" role="region" aria-label="Alterações pendentes">
    <div><span className="osk-save-dot" /><span><strong>{changedCount} alteração{changedCount === 1 ? "" : "ões"} pendente{changedCount === 1 ? "" : "s"}</strong><small>{sectionLabel}</small></span></div>
    <div><button className="osk-secondary-button osk-secondary-button--small" onClick={onDiscard} disabled={saving}><RotateCcw size={14} />Descartar</button><button className="osk-primary-button osk-primary-button--small" onClick={onSave} disabled={saving}><Save size={14} />{saving ? "Salvando..." : "Salvar mudanças"}</button></div>
  </div>;
}
