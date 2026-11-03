import { Check, ChevronDown, FileText, LockKeyhole, ShieldCheck, Users, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { normalizeDashboardFormField } from "../app/dashboardFieldValues";
import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardTemplateVariables,
} from "../types/dashboard";
import { DashboardFieldControl } from "./DashboardFieldControl";
import { DiscordRichText } from "./message-editor/DiscordRichText";
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
  const renderEditor = (editor: DashboardMessageEditorDefinition) => {
    const editorFields = [...editor.fieldIds, ...(editor.senderFieldIds ?? [])].map((id) => allFields.find((field) => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field));
    const changed = editorFields.some((field) => !sectionEditorValuesEqual(values[field.id], draft[field.id]));
    return <MessagePreviewCard key={editor.id} sectionId={sectionId} editor={editor} fields={editorFields} draft={draft} guildOptions={guildOptions} changed={changed} variables={metadata.variables} onOpen={onOpenEditor} />;
  };
  if (sectionId === "forms" && group === "Aprovação") {
    const approvalEditors = new Set(["forms-approve-dm", "forms-reject-dm"]);
    return <div className="osk-message-group-panel"><FormsApprovalSettings fields={settingsFields} draft={draft} renderFields={renderFields} renderMessage={kind => {
      const editor = editors.find(item => item.id === `forms-${kind}-dm`);
      return editor ? renderEditor(editor) : null;
    }} />{editors.filter(editor => !approvalEditors.has(editor.id)).map(renderEditor)}</div>;
  }

  const isWelcomeEntry = sectionId === "welcome" && group === "Mensagem de entrada";
  const afterEntry = isWelcomeEntry ? settingsFields.filter(field => field.id === "welcome.delete_on_leave_enabled") : [];
  const beforeMessage = settingsFields.filter(field => !afterEntry.includes(field));

  return <div className="osk-message-group-panel">
    {!enabled && <div className="osk-inline-note">O envio está desativado. Você pode editar e salvar a mensagem antes de ativá-lo.</div>}
    {beforeMessage.length > 0 && <div className="osk-message-group-settings" data-welcome-entry={isWelcomeEntry || undefined}>
      {isWelcomeEntry && <h3 className="osk-detail-heading">Destino e formato</h3>}
      {renderFields(beforeMessage)}
    </div>}
    <div className="osk-message-launcher-list">
      {editors.map(renderEditor)}
    </div>
    {afterEntry.length > 0 && <section className="osk-welcome-after-entry"><h3 className="osk-detail-heading">Após a entrada</h3>{renderFields(afterEntry)}</section>}
  </div>;
}

export function TicketAttendancePanel({ fields, draft, guildOptions, renderFields }: {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const organization = fields.filter((field) => field.id.includes(".channels."));
  const generalTeam = fields.filter((field) => field.id === "tickets.roles.staff_role_id");
  const specializedTeams = fields.filter((field) => field.id.startsWith("tickets.roles.") && field.id !== "tickets.roles.staff_role_id");
  const defaultTeam = String(draft["tickets.roles.staff_role_id"] || "");
  const defaultName = guildOptions?.roles.find(role => role.id === defaultTeam)?.name;
  return <div className="osk-settings-groups osk-attendance-details">
    {organization.length > 0 && <section><header><Users size={17} /><span><strong>Canais do atendimento</strong><small>Onde criar tickets e registrar o que aconteceu.</small></span></header>{renderFields(organization)}</section>}
    {generalTeam.length > 0 && <section><header><ShieldCheck size={17} /><span><strong>Equipe padrão</strong><small>Atende os fluxos sem uma equipe própria.</small></span></header>{renderFields(generalTeam.map(field => ({ ...field, label: "Cargo da equipe padrão" })))}</section>}
    {specializedTeams.length > 0 && <section><header><Users size={17} /><span><strong>Equipes por assunto</strong><small>{defaultTeam ? `Use Nenhum para manter ${defaultName ? `@${defaultName}` : "a equipe padrão"}.` : "Selecione a equipe padrão acima ou escolha um cargo por assunto."}</small></span></header>{renderFields(specializedTeams.map(field => ({ ...field, label: field.label.replace(/^Staff de /, "").replace(/^outros$/, "Outros").replace(/^parcerias$/, "Parcerias").replace(/^denúncias$/, "Denúncias") })))}</section>}
    {fields.filter(field => !organization.includes(field) && !generalTeam.includes(field) && !specializedTeams.includes(field)).length > 0 && renderFields(fields.filter(field => !organization.includes(field) && !generalTeam.includes(field) && !specializedTeams.includes(field)))}
  </div>;
}

function FormsApprovalSettings({ fields, draft, renderFields, renderMessage }: {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
  renderMessage(kind: "approve" | "reject"): ReactNode;
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
    return <span className="osk-action-button-preview" data-style={style}>{emoji && <b><DiscordRichText text={emoji} compact /></b>}{label}</span>;
  };
  const assigned = new Set([enabledField, roleField, ...approveFields, ...rejectFields].filter(Boolean));
  const remaining = fields.filter(field => !assigned.has(field));
  return <div className="osk-approval-settings">
    {enabledField && renderFields([enabledField])}
    {!enabled && <div className="osk-inline-note">A aprovação está desativada. Os ajustes abaixo serão usados quando você ativá-la.</div>}
    <>
      {roleField && <section className="osk-approval-settings__result"><header><LockKeyhole size={17} /><span><strong>Resultado da aprovação</strong><small>Cargo concedido quando a equipe aprova a resposta.</small></span></header>{renderFields([roleField])}</section>}
      <div className="osk-approval-settings__buttons">
        <section><header><span><Check size={17} /><strong>Ao aprovar</strong></span>{buttonPreview("approve")}</header>{renderFields(approveFields)}{renderMessage("approve")}</section>
        <section><header><span><X size={17} /><strong>Ao rejeitar</strong></span>{buttonPreview("reject")}</header>{renderFields(rejectFields)}{renderMessage("reject")}</section>
      </div>
      {remaining.length > 0 && renderFields(remaining)}
    </>
  </div>;
}

export function FormsQuestionPanel({ fields, draft, renderFields }: {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const questionField = fields.find(field => field.type === "form_fields");
  const questions = questionField && Array.isArray(draft[questionField.id])
    ? (draft[questionField.id] as unknown[]).map(normalizeDashboardFormField).filter(question => question.enabled).slice(0, 5)
    : [];
  const title = String(draft["forms.modal.title"] || "Formulário");
  return <div className="osk-question-layout">
    <section className="osk-form-live-preview" data-open={previewOpen || undefined}>
      <button type="button" className="osk-form-live-preview__toggle" onClick={() => setPreviewOpen(open => !open)} aria-expanded={previewOpen} aria-controls="forms-modal-preview">
        <FileText size={18} aria-hidden="true" /><span><strong>Como aparece no Discord</strong><small>{questions.length} pergunta{questions.length === 1 ? "" : "s"} visíve{questions.length === 1 ? "l" : "is"}</small></span><ChevronDown size={17} aria-hidden="true" />
      </button>
      {previewOpen && <div className="osk-form-modal-preview" id="forms-modal-preview" role="group" aria-label="Prévia do formulário">
        <header><strong>{title}</strong><X size={18} aria-hidden="true" /></header>
        <div className="osk-form-modal-preview__fields">
          {questions.map(question => <div key={question.id} className="osk-form-modal-preview__field"><strong>{question.label}{question.required && <span aria-label="Obrigatória"> *</span>}</strong><div data-long={question.long || undefined}>{question.placeholder || (question.long ? "Resposta longa" : "Resposta curta")}</div></div>)}
          {!questions.length && <p>Ative uma pergunta para incluí-la no formulário.</p>}
        </div>
        <footer><span>Cancelar</span><strong>Enviar</strong></footer>
      </div>}
    </section>
    {renderFields(fields)}
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
