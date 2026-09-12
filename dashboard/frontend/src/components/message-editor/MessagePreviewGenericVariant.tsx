import type { CSSProperties } from "react";
import { AccentControl, FieldText, ImageSlot } from "./MessagePreviewPrimitives";
import { previewColor } from "./messagePreviewModel";
import type { PreviewCoreProps } from "./messagePreviewTypes";

export function GenericMessagePreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const textFields = fields.filter((field) => field.type === "text" || field.type === "textarea");
  const imageFields = fields.filter((field) => field.type === "url" && /(image|media|banner|avatar)/i.test(`${field.id} ${field.label}`));
  const colorField = fields.find((field) => field.type === "color");
  const titleField = textFields.find((field) => /(?:^|\.)(title)$/.test(field.id) || /título/i.test(field.label));
  const footerField = textFields.find((field) => /(?:^|\.)(footer|footer_text)$/.test(field.id) || /rodapé/i.test(field.label));
  const bodyFields = textFields.filter((field) => field !== titleField && field !== footerField && !/(emoji|button|placeholder)/i.test(`${field.id} ${field.label}`));
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  if (!textFields.length && !imageFields.length && !interactive) return <div className="osk-message-preview__placeholder">Adicione conteúdo para começar.</div>;
  return (
    <div className="osk-message-preview__message-card" style={style}>
      {interactive && <AccentControl field={colorField} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
      {titleField && <FieldText {...textProps} field={titleField} className="osk-message-preview__card-title" placeholder="Adicionar título" />}
      {bodyFields.map((field) => <FieldText key={field.id} {...textProps} field={field} className="osk-message-preview__body" placeholder="Adicionar mensagem" />)}
      {imageFields.map((field) => <ImageSlot key={field.id} urlField={field} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__generic-image" alt={field.label} fallbackLabel={field.label} />)}
      {footerField && <FieldText {...textProps} field={footerField} className="osk-message-preview__card-footer" placeholder="Adicionar rodapé" />}
    </div>
  );
}
