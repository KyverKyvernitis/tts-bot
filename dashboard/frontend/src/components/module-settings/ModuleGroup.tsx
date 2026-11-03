import type { DashboardFieldDefinition, DashboardMessageEditorDefinition, DashboardOptionsPayload, DashboardSectionDefinition, DashboardTemplateVariables } from "../../types/dashboard";
import { FieldsPanel, FormsQuestionPanel, MessageGroupPanel, TicketAttendancePanel } from "../SectionEditorPanels";
import { TicketFlowEditor } from "../TicketFlowEditor";
import { TicketPermissionsEditor } from "../TicketPermissionsEditor";
import { ColorRolesPanelManager } from "../color-roles/ColorRolesPanelManager";
import { sectionGroupIcon } from "../sectionGroupPresentation";
import { BirthdayTimeField } from "./BirthdayTimeField";
import { TtsVoiceSettings } from "./TtsVoiceSettings";
import { AdvancedSettings } from "./AdvancedSettings";

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
  else if (section.id === "tickets" && group === "Atendimento") content = <TicketAttendancePanel fields={fields} draft={draft} guildOptions={guildOptions} renderFields={renderFields} />;
  else if (section.id === "tickets" && group === "Fluxos") content = <TicketFlowEditor fields={fields} draft={draft} renderFields={renderFields} onChange={onChange} />;
  else if (section.id === "tickets" && group === "Permissões") content = <TicketPermissionsEditor fields={fields} draft={draft} renderFields={renderFields} onChange={onChange} />;
  else if (section.id === "forms" && group === "Perguntas") content = <FormsQuestionPanel fields={fields} draft={draft} renderFields={renderFields} />;
  else if (section.id === "welcome" && group === "Aparência") {
    const publicEditorFields = new Set(section.groupMetadata?.["Mensagem de entrada"]?.editors?.flatMap(editor => editor.fieldIds) || []);
    const remaining = fields.filter(field => !publicEditorFields.has(field.id));
    const publicMode = String(draft["welcome.render_mode"] || "components_v2");
    const privateMode = String(draft["welcome.dm_render_mode"] || "components_v2");
    // These same values can still affect the private message, even when its
    // sending toggle is off. Keep them editable while preparing that message.
    const shared = publicMode !== "components_v2" && privateMode !== "normal"
      ? fields.filter(field => publicEditorFields.has(field.id))
      : publicMode === "normal" && Boolean(draft["welcome.decorative_emoji_enabled"])
        ? fields.filter(field => ["welcome.accent_color", "welcome.accent_color_mode"].includes(field.id))
        : [];
    content = <div className="osk-welcome-appearance">
      {remaining.length > 0 && renderFields(remaining)}
      {shared.length > 0 && <AdvancedSettings title="Ajustes compartilhados" summary={privateMode !== "normal" ? "Usados também pela mensagem privada." : "Cor usada pelos emojis decorativos."} fields={shared} values={values} draft={draft}>
        {renderFields(shared)}
      </AdvancedSettings>}
    </div>;
  }
  else if (metadata?.kind === "message") content = <MessageGroupPanel {...common} sectionId={section.id} group={group} fields={fields} allFields={section.fields} metadata={metadata} renderFields={renderFields} onOpenEditor={onOpenEditor} />;
  else content = renderFields(fields);
  if (!fields.length) return null;
  if (section.id === "tts" && group === "Voz") return <TtsVoiceSettings {...common} fields={fields} />;
  if (section.id === "tickets" && group === "Textos") return <AdvancedSettings title="Mensagens padrão e encerramento" summary="Textos usados durante o atendimento e ao fechar um ticket." fields={fields} values={values} draft={draft} icon={Icon}>{content}</AdvancedSettings>;
  return <section className="osk-settings-card" data-group={group}>
    <header><Icon size={19} aria-hidden="true" /><h2>{group === "Geral" && section.id === "birthday" ? "Preferências do cadastro" : group}</h2></header>
    {content}
  </section>;
}
