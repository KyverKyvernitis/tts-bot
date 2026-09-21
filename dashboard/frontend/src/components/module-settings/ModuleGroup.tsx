import type { DashboardFieldDefinition, DashboardMessageEditorDefinition, DashboardOptionsPayload, DashboardSectionDefinition, DashboardTemplateVariables } from "../../types/dashboard";
import { FieldsPanel, MessageGroupPanel, TicketAttendancePanel } from "../SectionEditorPanels";
import { TicketFlowEditor } from "../TicketFlowEditor";
import { TicketPermissionsEditor } from "../TicketPermissionsEditor";
import { ColorRolesPanelManager } from "../color-roles/ColorRolesPanelManager";
import { sectionGroupIcon } from "../sectionGroupPresentation";
import { BirthdayTimeField } from "./BirthdayTimeField";
import { TtsVoiceSettings } from "./TtsVoiceSettings";

export interface ModuleGroupProps {
  section: DashboardSectionDefinition; group: string; values: Record<string, unknown>; draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void;
  onOpenEditor(editor: DashboardMessageEditorDefinition, variables?: DashboardTemplateVariables): void;
}
export function ModuleGroup({ section, group, values, draft, guildOptions, onChange, onOpenEditor }: ModuleGroupProps) {
  const fields = section.fields.filter(field => (field.group || "Geral") === group);
  const metadata = section.groupMetadata?.[group];
  const Icon = sectionGroupIcon(group);
  const common = { values, draft, guildOptions, onChange };
  const renderFields = (items: DashboardFieldDefinition[]) => {
    const hasTime = items.some(field => field.id === "birthday.announce_hour") && items.some(field => field.id === "birthday.announce_minute");
    const remaining = hasTime ? items.filter(field => !["birthday.announce_hour", "birthday.announce_minute"].includes(field.id)) : items;
    return <>{hasTime && <BirthdayTimeField fields={items} draft={draft} onChange={onChange} />}{remaining.length > 0 && <FieldsPanel {...common} sectionId={section.id} fields={remaining} />}</>;
  };
  let content;
  if (section.id === "color_roles" && group === "Painel") content = <ColorRolesPanelManager {...common} fields={fields} onOpenEditor={onOpenEditor} />;
  else if (section.id === "tickets" && group === "Atendimento") content = <TicketAttendancePanel fields={fields} renderFields={renderFields} />;
  else if (section.id === "tickets" && group === "Fluxos") content = <TicketFlowEditor fields={fields} draft={draft} renderFields={renderFields} onChange={onChange} />;
  else if (section.id === "tickets" && group === "Permissões") content = <TicketPermissionsEditor fields={fields} draft={draft} renderFields={renderFields} onChange={onChange} />;
  else if (metadata?.kind === "message") content = <MessageGroupPanel {...common} sectionId={section.id} group={group} fields={fields} allFields={section.fields} metadata={metadata} renderFields={renderFields} onOpenEditor={onOpenEditor} />;
  else content = renderFields(fields);
  if (!fields.length) return null;
  if (section.id === "tts" && group === "Voz") return <TtsVoiceSettings {...common} fields={fields} />;
  if (section.id === "tickets" && group === "Textos") return <details className="osk-module-advanced"><summary><Icon size={17} />Mensagens padrão e encerramento</summary><div>{content}</div></details>;
  return <section className="osk-settings-card" data-group={group}>
    <header><Icon size={19} aria-hidden="true" /><h2>{group === "Geral" && section.id === "birthday" ? "Preferências do cadastro" : group}</h2></header>
    {content}
  </section>;
}
