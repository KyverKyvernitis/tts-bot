import { useMemo, useState } from "react";
import type { DashboardFieldDefinition, DashboardOptionsPayload, DashboardSectionDefinition } from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { MessageEditor } from "./message-editor";
import { sectionEditorValuesEqual } from "./sectionEditorModel";
import { useSectionMessageEditor } from "./useSectionMessageEditor";
import { moduleAreasFor } from "./module-settings/moduleAreas";
import { ModuleNavigation } from "./module-settings/ModuleNavigation";
import { ModuleSettingsHeader } from "./module-settings/ModuleSettingsHeader";
import { ModuleGroup } from "./module-settings/ModuleGroup";
import { EconomySettings } from "./module-settings/EconomySettings";
import { ModulePreviewContext } from "./module-settings/ModulePreviewContext";
import { useFieldFeedback } from "./module-settings/FieldFeedback";
import { revealModuleField } from "./module-settings/revealModuleField";

interface SectionEditorProps {
  section: DashboardSectionDefinition; module: DashboardVisualModule | null;
  values: Record<string, unknown>; draft: Record<string, unknown>; guildOptions: DashboardOptionsPayload | null;
  previewBotName?: string; previewBotAvatarUrl?: string | null; previewGuildName?: string; previewGuildAvatarUrl?: string | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void; onMessageEditorActiveChange?(active: boolean): void; onBack(): void;
}

export function SectionEditor({ section, module, values, draft, guildOptions, previewBotName, previewBotAvatarUrl, previewGuildName, previewGuildAvatarUrl, onChange, onMessageEditorActiveChange, onBack }: SectionEditorProps) {
  const areas = useMemo(() => moduleAreasFor(section), [section]);
  const [selectedArea, setSelectedArea] = useState(areas[0]?.id || "");
  const [focusFieldId, setFocusFieldId] = useState<string | null>(null);
  const selected = areas.find(area => area.id === selectedArea) || areas[0];
  const { errors } = useFieldFeedback();
  const changedAreas = new Set(areas.filter(area => section.fields.some(field => area.groups.includes(field.group || "Geral") && !sectionEditorValuesEqual(values[field.id], draft[field.id]))).map(area => area.id));
  const { activeEditor, openMessageEditor, finishEditor, closeEditorDiscard } = useSectionMessageEditor({ section, draft, onChange, onActiveChange: onMessageEditorActiveChange });
  const identity = { botName: previewBotName, botAvatarUrl: previewBotAvatarUrl, guildName: previewGuildName, guildAvatarUrl: previewGuildAvatarUrl };
  const fieldErrors = section.fields.filter(field => errors[field.id]);
  const openEditor: typeof openMessageEditor = (editor, variables) => { setFocusFieldId(null); openMessageEditor(editor, variables); };

  function revealField(field: DashboardFieldDefinition) {
    const area = areas.find(item => item.groups.includes(field.group || "Geral"));
    if (area) setSelectedArea(area.id);
    const editor = Object.values(section.groupMetadata || {}).flatMap(item => item.editors || []).find(item => [...item.fieldIds, ...(item.senderFieldIds || [])].includes(field.id));
    if (editor) { setFocusFieldId(field.id); openMessageEditor(editor); }
    else revealModuleField(field.id);
  }

  return <ModulePreviewContext.Provider value={identity}>
    <section className="osk-dashboard-page osk-section-page osk-module-settings" data-module={section.id} aria-hidden={activeEditor ? true : undefined}>
      <ModuleSettingsHeader section={section} module={module} draft={draft} onChange={onChange} onBack={onBack} />
      {fieldErrors.length > 0 && <div className="osk-module-errors" role="alert"><strong>Revise antes de salvar</strong>{fieldErrors.map(field => <button key={field.id} type="button" onClick={() => revealField(field)}>{field.label}: {errors[field.id]}</button>)}</div>}
      {areas.length > 1 && <ModuleNavigation sectionId={section.id} areas={areas} selected={selected?.id || ""} changed={changedAreas} onSelect={setSelectedArea} />}
      {selected && <div className="osk-module-area" role={areas.length > 1 ? "tabpanel" : undefined} id={`module-area-${section.id}-${selected.id}`} aria-labelledby={areas.length > 1 ? `module-tab-${section.id}-${selected.id}` : undefined}>
        {section.id === "economy" ? <EconomySettings fields={section.fields} draft={draft} guildOptions={guildOptions} onChange={onChange} /> : selected.groups.map(group => <ModuleGroup key={group} section={section} group={group} values={values} draft={draft} guildOptions={guildOptions} onChange={onChange} onOpenEditor={openEditor} />)}
      </div>}
    </section>
    {activeEditor && <MessageEditor focusFieldId={focusFieldId} editorId={activeEditor.id} sectionId={section.id} sectionLabel={section.label} groupLabel={activeEditor.label} description={activeEditor.description} fields={activeEditor.fields} baseline={activeEditor.baseline} draft={draft} guildOptions={guildOptions} {...identity} senderFieldIds={activeEditor.senderFieldIds} presentation={activeEditor.presentation} variables={activeEditor.variables} onChange={onChange} onApply={finishEditor} onDiscard={closeEditorDiscard} />}
  </ModulePreviewContext.Provider>;
}
