import { Check, LockKeyhole, ShieldCheck, Users, X } from "lucide-react";
import type { ReactNode } from "react";
import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardTemplateVariables,
} from "../types/dashboard";
import { DashboardFieldControl } from "./DashboardFieldControl";
import { MessagePreviewCard } from "./module-settings/MessagePreviewCard";
import {
  createLegacyMessageEditor,
  sectionEditorEnabled,
  sectionEditorFieldVisible,
  sectionEditorValuesEqual,
} from "./sectionEditorModel";

export function MessageGroupPanel({
  sectionId, group, fields, allFields = fields, metadata, values, draft, guildOptions, renderFields, onOpenEditor,
}: {
  sectionId: string;
  group: string;
  fields: DashboardFieldDefinition[];
  allFields?: DashboardFieldDefinition[];
  metadata: NonNullable<DashboardSectionDefinition["groupMetadata"]>[string];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
  onOpenEditor(editor: DashboardMessageEditorDefinition, fallbackVariables?: DashboardTemplateVariables): void;
}) {
  const editors = metadata.editors?.length ? metadata.editors : [createLegacyMessageEditor(group, fields)];
  const editorFieldIds = new Set(editors.flatMap((editor) => [...editor.fieldIds, ...(editor.senderFieldIds ?? [])]));
  const settingsIds = new Set(metadata.settingsFieldIds ?? fields.filter((field) => !editorFieldIds.has(field.id)).map((field) => field.id));
  const settingsFields = fields.filter((field) => settingsIds.has(field.id));
  const enabled = sectionEditorEnabled(sectionId, group, draft);

  return <div className="osk-message-group-panel">
    {!enabled && <div className="osk-inline-note">Você pode preparar estas mensagens agora. O envio acompanha a opção configurada acima.</div>}
    {settingsFields.length > 0 && <div className="osk-message-group-settings">
      {sectionId === "forms" && group === "Aprovação"
        ? <FormsApprovalSettings fields={settingsFields} draft={draft} renderFields={renderFields} />
        : renderFields(settingsFields)}
    </div>}
    <div className="osk-message-launcher-list">
      {editors.map((editor) => {
        const editorFields = [...editor.fieldIds, ...(editor.senderFieldIds ?? [])].map((id) => allFields.find((field) => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field));
        const changed = editorFields.some((field) => !sectionEditorValuesEqual(values[field.id], draft[field.id]));
        return <MessagePreviewCard key={editor.id} sectionId={sectionId} editor={editor} fields={editorFields} draft={draft} guildOptions={guildOptions} changed={changed} variables={metadata.variables} onOpen={onOpenEditor} />;
      })}
    </div>
  </div>;
}

export function TicketAttendancePanel({ fields, renderFields }: {
  fields: DashboardFieldDefinition[];
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const organization = fields.filter((field) => field.id.includes(".channels."));
  const generalTeam = fields.filter((field) => field.id === "tickets.roles.staff_role_id");
  const specializedTeams = fields.filter((field) => field.id.startsWith("tickets.roles.") && field.id !== "tickets.roles.staff_role_id");
  return <div className="osk-settings-groups">
    {organization.length > 0 && <section><header><Users size={17} /><span><strong>Organização</strong><small>Categoria e canais usados pelo atendimento.</small></span></header>{renderFields(organization)}</section>}
    {generalTeam.length > 0 && <section><header><ShieldCheck size={17} /><span><strong>Equipe geral</strong><small>Cargo usado quando um fluxo não possui equipe específica.</small></span></header>{renderFields(generalTeam)}</section>}
    {specializedTeams.length > 0 && <section><header><Users size={17} /><span><strong>Equipes específicas</strong><small>Deixe como Nenhum para usar a equipe geral.</small></span></header>{renderFields(specializedTeams)}</section>}
  </div>;
}

function FormsApprovalSettings({ fields, draft, renderFields }: {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const enabledField = fields.find((field) => field.id === "forms.approval.enabled");
  const roleField = fields.find((field) => field.id === "forms.approval.role_id");
  const approveFields = fields.filter((field) => /forms\.approval\.approve_(label|emoji|style)$/.test(field.id));
  const rejectFields = fields.filter((field) => /forms\.approval\.reject_(label|emoji|style)$/.test(field.id));
  const enabled = Boolean(draft["forms.approval.enabled"]);
  const buttonPreview = (kind: "approve" | "reject") => {
    const label = String(draft[`forms.approval.${kind}_label`] || (kind === "approve" ? "Aprovar" : "Rejeitar"));
    const emoji = String(draft[`forms.approval.${kind}_emoji`] || "");
    const style = String(draft[`forms.approval.${kind}_style`] || (kind === "approve" ? "success" : "danger"));
    return <span className="osk-action-button-preview" data-style={style}>{emoji && <b>{emoji}</b>}{label}</span>;
  };
  return <div className="osk-approval-settings">
    {enabledField && renderFields([enabledField])}
    {!enabled && <div className="osk-inline-note">Prepare o cargo e os botões abaixo. Eles serão usados ao habilitar a aprovação.</div>}
    <>
      {roleField && <section className="osk-approval-settings__result"><header><LockKeyhole size={17} /><span><strong>Resultado da aprovação</strong><small>Cargo concedido quando a equipe aprova a resposta.</small></span></header>{renderFields([roleField])}</section>}
      <div className="osk-approval-settings__buttons">
        <section><header><span><Check size={17} /><strong>Botão Aprovar</strong></span>{buttonPreview("approve")}</header>{renderFields(approveFields)}</section>
        <section><header><span><X size={17} /><strong>Botão Rejeitar</strong></span>{buttonPreview("reject")}</header>{renderFields(rejectFields)}</section>
      </div>
    </>
  </div>;
}

export function FieldsPanel({
  sectionId, fields, values, draft, guildOptions, onChange,
}: {
  sectionId: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}) {
  const ignoredEnabledField = fields.find((field) => field.id === "tts.ignored_tts_role_enabled");
  const visibleFields = fields.filter((field) => sectionEditorFieldVisible(sectionId, field, draft));

  if (visibleFields.length === 0) return <div className="osk-inline-note">Nenhuma opção adicional é necessária para a configuração atual.</div>;

  return <div className="osk-compact-fields">
    {visibleFields.map((field) => {
      const isIgnoredRole = field.id === "tts.ignored_tts_role_id" && Boolean(ignoredEnabledField);
      const changed = !sectionEditorValuesEqual(draft[field.id], values[field.id])
        || (isIgnoredRole && !sectionEditorValuesEqual(draft[ignoredEnabledField!.id], values[ignoredEnabledField!.id]));
      const isComplex = ["textarea", "role_multi", "string_list", "form_fields", "color_slots"].includes(field.type);
      const displayField = isIgnoredRole
        ? { ...field, label: "Ignorar mensagens deste cargo", description: "Selecione Nenhum para desativar esta regra." }
        : field;
      const controlValue = isIgnoredRole && !Boolean(draft[ignoredEnabledField!.id]) ? "" : draft[field.id];
      const handleChange = (changedField: DashboardFieldDefinition, raw: unknown) => {
        onChange(changedField, raw);
        if (changedField.id === "tts.ignored_tts_role_id" && ignoredEnabledField) onChange(ignoredEnabledField, Boolean(String(raw || "").trim()));
      };

      return <div key={field.id} className="osk-compact-field" data-type={field.type} data-changed={changed || undefined} data-complex={isComplex || undefined}>
        <div className="osk-compact-field-copy">
          <strong>{displayField.label}</strong>
          {displayField.description && <small>{displayField.description}</small>}
        </div>
        <div className="osk-compact-field-control"><DashboardFieldControl field={displayField} value={controlValue} guildOptions={guildOptions} onChange={handleChange} /></div>
      </div>;
    })}
  </div>;
}
