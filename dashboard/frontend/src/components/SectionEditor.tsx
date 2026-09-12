import { ArrowLeft, ChevronDown, Settings } from "lucide-react";
import { useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { MessageEditor } from "./message-editor";
import { TicketFlowEditor } from "./TicketFlowEditor";
import { TicketPermissionsEditor } from "./TicketPermissionsEditor";
import { ColorRolesPanelManager } from "./color-roles/ColorRolesPanelManager";
import { sectionEditorValuesEqual } from "./sectionEditorModel";
import { sectionGroupDescription, sectionGroupIcon } from "./sectionGroupPresentation";
import { useSectionMessageEditor } from "./useSectionMessageEditor";
import { FieldsPanel, MessageGroupPanel, TicketAttendancePanel } from "./SectionEditorPanels";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  previewBotName?: string;
  previewBotAvatarUrl?: string | null;
  previewGuildName?: string;
  previewGuildAvatarUrl?: string | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onMessageEditorActiveChange?(active: boolean): void;
  onBack(): void;
}

export function SectionEditor({
  section, module, values, draft, guildOptions, previewBotName, previewBotAvatarUrl,
  previewGuildName, previewGuildAvatarUrl, onChange, onMessageEditorActiveChange, onBack,
}: SectionEditorProps) {
  const Icon = module?.icon ?? Settings;
  const groups = useMemo(() => section.groups?.length ? section.groups : null, [section.groups]);
  const [openGroup, setOpenGroup] = useState<string | null>(() => section.groups?.[0] ?? null);
  const groupRefs = useRef<Record<string, HTMLElement | null>>({});
  const { activeEditor, openMessageEditor, finishEditor, closeEditorDiscard } = useSectionMessageEditor({
    section,
    draft,
    onChange,
    onActiveChange: onMessageEditorActiveChange,
  });

  function toggleGroup(group: string, scroll = false) {
    const next = openGroup === group ? null : group;
    setOpenGroup(next);
    if (next && scroll) {
      window.setTimeout(() => groupRefs.current[group]?.scrollIntoView({ behavior: "smooth", block: "start" }), 70);
    }
  }

  function renderFields(fields: DashboardFieldDefinition[]): ReactNode {
    return <FieldsPanel sectionId={section.id} fields={fields} values={values} draft={draft} guildOptions={guildOptions} onChange={onChange} />;
  }

  return <>
  <section className="osk-dashboard-page osk-section-page" aria-hidden={activeEditor ? true : undefined}>
    <button className="osk-page-back" onClick={onBack}><ArrowLeft size={16} />Módulos</button>
    <header className="osk-function-heading">
      <span className="osk-function-heading-icon"><Icon size={24} /></span>
      <div><h1>{section.label}</h1><p>{module?.description || section.description}</p></div>
    </header>

    {groups && groups.length > 3 && <nav className="osk-section-jump-nav" aria-label={`Áreas de ${section.label}`}>
      {groups.map((group) => <button key={group} type="button" data-active={openGroup === group || undefined} onClick={() => toggleGroup(group, true)}>{group}</button>)}
    </nav>}

    {groups ? (
      <div className="osk-accordion-list">
        {groups.map((group, index) => {
          const GroupIcon = sectionGroupIcon(group);
          const groupFields = section.fields.filter((field) => field.group === group);
          const changed = groupFields.filter((field) => !sectionEditorValuesEqual(values[field.id], draft[field.id])).length;
          const open = openGroup === group;
          const metadata = section.groupMetadata?.[group];
          const panelId = `section-panel-${section.id}-${index}`;
          return <article
            key={group}
            id={`section-group-${section.id}-${index}`}
            ref={(node) => { groupRefs.current[group] = node; }}
            className="osk-accordion"
            data-open={open || undefined}
            style={{ "--osk-card-index": index } as CSSProperties}
          >
            <button type="button" className="osk-accordion-trigger" onClick={() => toggleGroup(group)} aria-expanded={open} aria-controls={panelId}>
              <span className="osk-accordion-icon"><GroupIcon size={19} /></span>
              <span className="osk-accordion-copy"><strong>{group}</strong><small>{sectionGroupDescription(group)}</small></span>
              {changed > 0 && <em>Alterado</em>}
              <ChevronDown size={18} className="osk-accordion-chevron" />
            </button>
            {open && <div className="osk-accordion-panel" id={panelId}>
              <div className="osk-accordion-panel-inner">
                {section.id === "color_roles" && group === "Painel" ? (
                  <ColorRolesPanelManager
                    fields={groupFields}
                    values={values}
                    draft={draft}
                    guildOptions={guildOptions}
                    onChange={onChange}
                    onOpenEditor={(editor) => openMessageEditor(editor)}
                  />
                ) : section.id === "tickets" && group === "Atendimento" ? (
                  <TicketAttendancePanel fields={groupFields} renderFields={renderFields} />
                ) : section.id === "tickets" && group === "Fluxos" ? (
                  <TicketFlowEditor fields={groupFields} draft={draft} renderFields={renderFields} onChange={onChange} />
                ) : section.id === "tickets" && group === "Permissões" ? (
                  <TicketPermissionsEditor fields={groupFields} draft={draft} renderFields={renderFields} onChange={onChange} />
                ) : metadata?.kind === "message" ? (
                  <MessageGroupPanel
                    sectionId={section.id}
                    group={group}
                    fields={groupFields}
                    metadata={metadata}
                    values={values}
                    draft={draft}
                    guildOptions={guildOptions}
                    renderFields={renderFields}
                    onOpenEditor={openMessageEditor}
                  />
                ) : (
                  renderFields(groupFields)
                )}
              </div>
            </div>}
          </article>;
        })}
      </div>
    ) : (
      <div className="osk-settings-panel">{renderFields(section.fields)}</div>
    )}
  </section>
  {activeEditor && <MessageEditor
    editorId={activeEditor.id}
    sectionId={section.id}
    sectionLabel={section.label}
    groupLabel={activeEditor.label}
    description={activeEditor.description}
    fields={activeEditor.fields}
    baseline={activeEditor.baseline}
    draft={draft}
    guildOptions={guildOptions}
    botName={previewBotName}
    botAvatarUrl={previewBotAvatarUrl}
    guildName={previewGuildName}
    guildAvatarUrl={previewGuildAvatarUrl}
    senderFieldIds={activeEditor.senderFieldIds}
    presentation={activeEditor.presentation}
    variables={activeEditor.variables}
    onChange={onChange}
    onApply={finishEditor}
    onDiscard={closeEditorDiscard}
  />}
  </>;
}

