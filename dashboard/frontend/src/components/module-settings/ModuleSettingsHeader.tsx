import { ArrowLeft, Settings } from "lucide-react";
import type { DashboardVisualModule } from "../../moduleCatalog";
import type { DashboardFieldDefinition, DashboardSectionDefinition } from "../../types/dashboard";
import { ModuleArtwork } from "../ModuleArtwork";
import { MODULE_ACTIONS } from "./moduleAreas";

interface Props { section: DashboardSectionDefinition; module: DashboardVisualModule | null; draft: Record<string, unknown>; onChange(field: DashboardFieldDefinition, value: unknown): void; onBack(): void }
export function ModuleSettingsHeader({ section, module, draft, onChange, onBack }: Props) {
  const enable = section.fields.find(field => field.group === "Ativação" && field.type === "boolean");
  return <>
    <button type="button" className="osk-page-back" onClick={onBack}><ArrowLeft size={15} />Módulos</button>
    <header className="osk-module-heading">
      <span className="osk-module-emblem"><ModuleArtwork moduleId={section.id} fallbackIcon={module?.icon || Settings} /></span>
      <h1>{section.label}</h1>
      {enable && <label className="osk-module-toggle"><span>{MODULE_ACTIONS[section.id] || enable.label}</span><span className="osk-switch"><input type="checkbox" checked={Boolean(draft[enable.id])} onChange={event => onChange(enable, event.target.checked)} /><span className="osk-switch-track" /></span></label>}
    </header>
  </>;
}
