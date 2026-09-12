import { Boxes } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { DiscordRichText } from "./DiscordRichText";
import { isValidPreviewUrl } from "./messageEditorUtils";
import { AccentControl, EditableRegion, FieldText, ImageSlot } from "./MessagePreviewPrimitives";
import { fieldString, fieldValue, findExact, normalizedColor, optionLabel } from "./messagePreviewModel";
import type { PreviewCoreProps } from "./messagePreviewTypes";

function V2BlockLabel({ children }: { children: ReactNode }) {
  return <span className="osk-v2-component-label"><Boxes size={11} />{children}</span>;
}

export function WelcomeComponentsV2Preview(props: PreviewCoreProps & { dm?: boolean }) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection, dm } = props;
  const titleField = dm ? findExact(fields, "welcome.dm.title") : findExact(fields, "welcome.public.title");
  const bodyField = dm ? findExact(fields, "welcome.dm.body") : findExact(fields, "welcome.public.body");
  const footerField = dm ? findExact(fields, "welcome.dm.footer") : findExact(fields, "welcome.public.footer");
  const styleField = findExact(fields, "welcome.style");
  const accentModeField = findExact(fields, "welcome.accent_color_mode");
  const accentField = findExact(fields, "welcome.accent_color");
  const mediaModeField = findExact(fields, "welcome.media_mode");
  const mediaUrlField = findExact(fields, "welcome.media_url");
  const styleValue = fieldString(styleField, draft) || String(draft["welcome.style"] || "complete");
  const accent = normalizedColor(fieldValue(accentField, draft) ?? draft["welcome.accent_color"]) ?? "#5865F2";
  const accentTarget = String(draft["welcome.accent_color_mode"] || fieldString(accentModeField, draft) || "fixed") === "fixed" && accentField ? accentField : accentModeField ?? accentField;
  const showMedia = !dm && styleValue === "complete";
  const showFooter = styleValue !== "compact";
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-v2-message">
      <div className="osk-v2-container" style={{ "--osk-message-accent": accent } as CSSProperties}>
        {interactive && <AccentControl field={accentTarget} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
        <div className="osk-v2-container__toolbar">
          <EditableRegion field={styleField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-structure-chip" placeholder="Estilo do container">
            <V2BlockLabel>Container · {optionLabel(styleField, draft) ?? "Completo"}</V2BlockLabel>
          </EditableRegion>
        </div>
        <div className="osk-v2-text-display osk-v2-text-display--title">
          <V2BlockLabel>Texto</V2BlockLabel>
          <FieldText {...textProps} field={titleField} className="osk-v2-text" placeholder="Adicionar título" />
        </div>
        <div className="osk-v2-text-display">
          <V2BlockLabel>Texto</V2BlockLabel>
          <FieldText {...textProps} field={bodyField} className="osk-v2-text" placeholder="Adicionar mensagem" />
        </div>
        {showMedia && (interactive || fieldString(mediaUrlField, draft) || optionLabel(mediaModeField, draft)) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-media-gallery">
              <V2BlockLabel>Galeria de mídia</V2BlockLabel>
              <ImageSlot urlField={mediaUrlField} modeField={mediaModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-media" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" />
            </div>
          </>
        )}
        {showFooter && (interactive || fieldString(footerField, draft).trim()) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-text-display osk-v2-text-display--footer">
              <V2BlockLabel>Texto</V2BlockLabel>
              <FieldText {...textProps} field={footerField} className="osk-v2-text" placeholder="Adicionar rodapé" />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function ComponentsV2Preview(props: PreviewCoreProps) {
  const { editorId, fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const titleField = fields.find((field) => /(?:^|\.)(title)$/.test(field.id));
  const footerField = fields.find((field) => /(?:^|\.)(footer|footer_text)$/.test(field.id));
  const bodyFields = fields.filter((field) => (field.type === "text" || field.type === "textarea") && field !== titleField && field !== footerField && !/(button|emoji|placeholder)/i.test(`${field.id} ${field.label}`));
  const colorField = fields.find((field) => field.type === "color");
  const mediaField = fields.find((field) => field.type === "url" && /(media|image)(?:_url)?$/i.test(field.id) && !/side/i.test(field.id));
  const sideMediaField = fields.find((field) => field.type === "url" && /side.*image|image.*side/i.test(field.id));
  const buttonLabelField = fields.find((field) => /button_label$|approve_label$|reject_label$/i.test(field.id));
  const buttonEmojiField = fields.find((field) => /button_emoji$|approve_emoji$|reject_emoji$/i.test(field.id));
  const buttonStyleField = fields.find((field) => /button_style$|approve_style$|reject_style$/i.test(field.id));
  const placeholderField = fields.find((field) => /placeholder$/i.test(field.id));
  const isApproveDm = editorId === "forms-approve-dm";
  const isRejectDm = editorId === "forms-reject-dm";
  const accent = normalizedColor(fieldValue(colorField, draft)) ?? (isApproveDm ? "#248046" : isRejectDm ? "#DA373C" : "#5865F2");
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };
  const isTicketPanel = editorId === "tickets-panel";
  const isFormsResponse = editorId === "forms-response";
  const hasAction = Boolean(buttonLabelField || placeholderField);

  return (
    <div className="osk-v2-message">
      <div className="osk-v2-container" style={{ "--osk-message-accent": accent } as CSSProperties}>
        {interactive && <AccentControl field={colorField} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
        <div className="osk-v2-container__toolbar"><V2BlockLabel>Container</V2BlockLabel></div>
        {(isApproveDm || isRejectDm) && (
          <div className="osk-v2-text-display osk-v2-text-display--title osk-v2-text-display--runtime">
            <V2BlockLabel>Texto fixo</V2BlockLabel>
            <div className="osk-v2-text">{isApproveDm ? "✅ Verificação aprovada" : "❌ Verificação rejeitada"}</div>
          </div>
        )}
        {isTicketPanel && sideMediaField ? (
          <div className="osk-v2-section">
            <div className="osk-v2-section__copy">
              <V2BlockLabel>Seção</V2BlockLabel>
              <FieldText {...textProps} field={titleField} className="osk-v2-text osk-v2-text--title" placeholder="Adicionar título" />
              {bodyFields.map((field) => <FieldText key={field.id} {...textProps} field={field} className="osk-v2-text" placeholder="Adicionar texto" />)}
            </div>
            <div className="osk-v2-section__accessory">
              <V2BlockLabel>Thumbnail</V2BlockLabel>
              <ImageSlot urlField={sideMediaField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-thumbnail" alt="Imagem lateral" fallbackLabel="Adicionar imagem lateral" />
            </div>
          </div>
        ) : (
          <>
            {titleField && <div className="osk-v2-text-display osk-v2-text-display--title"><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={titleField} className="osk-v2-text" placeholder="Adicionar título" /></div>}
            {bodyFields.map((field) => <div className="osk-v2-text-display" key={field.id}><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={field} className="osk-v2-text" placeholder="Adicionar texto" /></div>)}
          </>
        )}

        {isFormsResponse && (
          <div className="osk-v2-runtime-block" aria-label="Campos preenchidos em tempo de execução">
            <V2BlockLabel>Campos do formulário</V2BlockLabel>
            <strong>Nome</strong><span>Resposta do membro</span>
            <strong>Descrição</strong><span>Conteúdo enviado no formulário</span>
          </div>
        )}

        {mediaField && (interactive || isValidPreviewUrl(fieldString(mediaField, draft))) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-media-gallery"><V2BlockLabel>Galeria de mídia</V2BlockLabel><ImageSlot urlField={mediaField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-media" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" /></div>
          </>
        )}

        {footerField && (interactive || fieldString(footerField, draft).trim()) && (
          <><div className="osk-v2-separator" aria-hidden="true" /><div className="osk-v2-text-display osk-v2-text-display--footer"><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={footerField} className="osk-v2-text" placeholder="Adicionar rodapé" /></div></>
        )}

        {isFormsResponse && Boolean(draft["forms.approval.enabled"]) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-action-row osk-v2-action-row--runtime">
              <V2BlockLabel>Linha de ações</V2BlockLabel>
              <div className="osk-message-preview__button-row">
                <span data-style={String(draft["forms.approval.approve_style"] || "success")}>{String(draft["forms.approval.approve_emoji"] || "✅")} {String(draft["forms.approval.approve_label"] || "Aprovar")}</span>
                <span data-style={String(draft["forms.approval.reject_style"] || "danger")}>{String(draft["forms.approval.reject_emoji"] || "❌")} {String(draft["forms.approval.reject_label"] || "Rejeitar")}</span>
              </div>
            </div>
          </>
        )}

        {hasAction && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-action-row">
              <V2BlockLabel>Linha de ações</V2BlockLabel>
              {placeholderField ? (
                <EditableRegion field={placeholderField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Menu de seleção">
                  <div className="osk-message-preview__select-sim"><span>{fieldString(placeholderField, draft) || "Escolha uma opção"}</span><span>⌄</span></div>
                </EditableRegion>
              ) : (
                <EditableRegion field={buttonLabelField ?? buttonStyleField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Botão">
                  <div className="osk-message-preview__button-row"><span data-style={fieldString(buttonStyleField, draft) || "primary"}>{fieldString(buttonEmojiField, draft) && <><DiscordRichText text={fieldString(buttonEmojiField, draft)} guildOptions={guildOptions} compact /> </>}<DiscordRichText text={fieldString(buttonLabelField, draft) || "Continuar"} guildOptions={guildOptions} compact /></span></div>
                </EditableRegion>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
