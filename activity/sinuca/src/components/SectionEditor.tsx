import { ArrowLeft, ChevronRight, Settings } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { DashboardFieldControl, displayDashboardValue } from "./DashboardFieldControl";
import { MessageEditor } from "./message-editor";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  previewBotName?: string;
  previewBotAvatarUrl?: string | null;
  hasUnsavedChanges: boolean;
  applying: boolean;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onApply(): void | Promise<void>;
  onMessageEditorActiveChange?(active: boolean): void;
  onBack(): void;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function SectionEditor({
  section, module, values, draft, guildOptions, previewBotName, previewBotAvatarUrl,
  hasUnsavedChanges, applying, onChange, onApply, onMessageEditorActiveChange, onBack,
}: SectionEditorProps) {
  const Icon = module?.icon ?? Settings;
  const groups = section.groups?.length ? section.groups : null;
  const [activeGroup, setActiveGroup] = useState<string | null>(null);

  useEffect(() => setActiveGroup(null), [section.id]);
  const insideGroup = Boolean(groups && activeGroup);
  const fieldsToShow = useMemo(() => groups ? section.fields.filter((field) => field.group === activeGroup) : section.fields, [activeGroup, groups, section.fields]);
  const groupMetadata = activeGroup ? section.groupMetadata?.[activeGroup] : undefined;
  const isMessageGroup = insideGroup && groupMetadata?.kind === "message";

  useEffect(() => {
    onMessageEditorActiveChange?.(isMessageGroup);
    return () => onMessageEditorActiveChange?.(false);
  }, [isMessageGroup, onMessageEditorActiveChange]);

  if (isMessageGroup && activeGroup) {
    return <MessageEditor
      sectionId={section.id}
      sectionLabel={section.label}
      groupLabel={activeGroup}
      fields={fieldsToShow}
      values={values}
      draft={draft}
      guildOptions={guildOptions}
      botName={previewBotName}
      botAvatarUrl={previewBotAvatarUrl}
      variables={groupMetadata?.variables}
      hasUnsavedChanges={hasUnsavedChanges}
      applying={applying}
      onChange={onChange}
      onApply={onApply}
      onBack={() => setActiveGroup(null)}
    />;
  }

  return <section className="osk-dashboard-page osk-section-page">
    <button className="osk-page-back" onClick={() => insideGroup ? setActiveGroup(null) : onBack()}><ArrowLeft size={15} />{insideGroup ? section.label : "Início"}</button>
    <header className="osk-section-header osk-section-header--simple">
      <span className="osk-section-header-icon"><Icon size={23} /></span>
      <div>
        <span className="osk-kicker">{insideGroup ? section.label : "Configuração"}</span>
        <h1>{insideGroup ? activeGroup : section.label}</h1>
        <p>{insideGroup ? "Ajuste somente as opções que quiser usar." : module?.description || section.description}</p>
      </div>
    </header>

    {groups && !insideGroup ? (
      <div className="osk-group-grid osk-group-grid--simple">
        {groups.map((group) => {
          const groupFields = section.fields.filter((field) => field.group === group);
          const changed = groupFields.filter((field) => !valuesEqual(values[field.id], draft[field.id])).length;
          return <button key={group} className="osk-group-card osk-group-card--simple" onClick={() => setActiveGroup(group)}>
            <span className="osk-group-card-icon"><Icon size={19} /></span>
            <span><strong>{group}</strong><small>Abrir ajustes</small></span>
            {changed > 0 && <em>{changed} pendente{changed === 1 ? "" : "s"}</em>}
            <ChevronRight size={18} />
          </button>;
        })}
      </div>
    ) : (
      <div className="osk-fields-grid">
        {fieldsToShow.map((field) => {
          const changed = !valuesEqual(draft[field.id], values[field.id]);
          return <article key={field.id} className="osk-field-card" data-changed={changed || undefined} data-wide={["textarea", "role_multi", "string_list", "form_fields", "color_slots"].includes(field.type) || undefined}>
            <header><div><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div>{changed && <span>Alterado</span>}</header>
            <DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} />
            {changed && <footer>Valor atual: <strong>{displayDashboardValue(field, values[field.id], guildOptions)}</strong></footer>}
          </article>;
        })}
      </div>
    )}
  </section>;
}
