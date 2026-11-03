import { ArrowLeft, Settings } from "lucide-react";
import type { DashboardVisualModule } from "../../moduleCatalog";
import type { DashboardFieldDefinition, DashboardSectionDefinition } from "../../types/dashboard";
import { ModuleArtwork } from "../ModuleArtwork";
import { MODULE_ACTIONS } from "./moduleAreas";
import { sectionEditorValuesEqual } from "../sectionEditorModel";

interface Props { section: DashboardSectionDefinition; module: DashboardVisualModule | null; values: Record<string, unknown>; draft: Record<string, unknown>; onChange(field: DashboardFieldDefinition, value: unknown): void; onBack(): void }
export function ModuleSettingsHeader({ section, module, values, draft, onChange, onBack }: Props) {
  const enable = section.fields.find(field => field.group === "Ativação" && field.type === "boolean");
  const changed = section.fields.some(field => !sectionEditorValuesEqual(values[field.id], draft[field.id]));
  const activationChanged = enable && !sectionEditorValuesEqual(values[enable.id], draft[enable.id]);
  const active = enable ? Boolean(draft[enable.id]) : null;
  return <>
    <div className="osk-module-breadcrumb">
      <button type="button" className="osk-page-back" onClick={onBack}><ArrowLeft size={15} aria-hidden="true" />Módulos</button>
      {changed && <span className="osk-module-pending">Alterações não salvas</span>}
    </div>
    <header className="osk-module-heading" data-pending={changed || undefined}>
      <span className="osk-module-emblem"><ModuleArtwork moduleId={section.id} fallbackIcon={module?.icon || Settings} /></span>
      <div className="osk-module-heading-copy"><h1>{section.label}</h1>{section.description && <p>{section.description}</p>}</div>
      {enable && <label className="osk-module-toggle">
        <span className="osk-module-toggle-copy"><strong>{MODULE_ACTIONS[section.id] || enable.label}</strong><small>{active ? "Ativado" : "Desativado"}{activationChanged ? " · não salvo" : ""}</small></span>
        <span className="osk-switch"><input type="checkbox" aria-label={MODULE_ACTIONS[section.id] || enable.label} checked={Boolean(draft[enable.id])} onChange={event => onChange(enable, event.target.checked)} /><span className="osk-switch-track" /></span>
      </label>}
    </header>
  </>;
}
