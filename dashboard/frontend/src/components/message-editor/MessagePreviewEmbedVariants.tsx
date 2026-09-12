import type { CSSProperties } from "react";
import { AccentControl, FieldText, ImageSlot, IconSlot } from "./MessagePreviewPrimitives";
import { fieldString, findExact, findField, normalizedColor, previewColor } from "./messagePreviewModel";
import type { PreviewCoreProps } from "./messagePreviewTypes";

export function EmbedPreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const contentField = findField(fields, [".embed.content"]);
  const authorField = findField(fields, [".embed.author_name"]);
  const authorIconUrlField = findField(fields, [".embed.author_icon_url"]);
  const authorIconModeField = findField(fields, [".embed.author_icon_mode"]);
  const titleField = findField(fields, [".embed.title"]);
  const descriptionField = findField(fields, [".embed.description"]);
  const footerField = findField(fields, [".embed.footer_text"]);
  const footerIconUrlField = findField(fields, [".embed.footer_icon_url"]);
  const footerIconModeField = findField(fields, [".embed.footer_icon_mode"]);
  const imageUrlField = findField(fields, [".embed.image_url"]);
  const imageModeField = findField(fields, [".embed.image_mode"]);
  const thumbnailUrlField = findField(fields, [".embed.thumbnail_url"]);
  const thumbnailModeField = findField(fields, [".embed.thumbnail_mode"]);
  const colorField = findField(fields, [".embed.color"]);
  const colorModeField = findField(fields, [".embed.color_mode"]);
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;
  const colorTarget = fieldString(colorModeField, draft) === "fixed" && colorField ? colorField : colorModeField ?? colorField;
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-message-preview__message">
      <FieldText {...textProps} field={contentField} className="osk-message-preview__content" placeholder="Adicionar conteúdo" />
      <div className="osk-message-preview__embed" style={style}>
        {interactive && <AccentControl field={colorTarget} selectedFieldId={selectedFieldId} onSelectField={onSelectField} label="Editar cor do embed" />}
        <div className="osk-message-preview__embed-main">
          <div className="osk-message-preview__author-row">
            <IconSlot urlField={authorIconUrlField} modeField={authorIconModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} alt="Ícone do autor" fallbackLabel="Ícone do autor" />
            <FieldText {...textProps} field={authorField} className="osk-message-preview__author" placeholder="Adicionar autor" />
          </div>
          <FieldText {...textProps} field={titleField} className="osk-message-preview__title" placeholder="Adicionar título" />
          <FieldText {...textProps} field={descriptionField} className="osk-message-preview__description" placeholder="Adicionar descrição" />
          <ImageSlot urlField={imageUrlField} modeField={imageModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__image" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" />
          <div className="osk-message-preview__footer-row">
            <IconSlot urlField={footerIconUrlField} modeField={footerIconModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} alt="Ícone do rodapé" fallbackLabel="Ícone do rodapé" />
            <FieldText {...textProps} field={footerField} className="osk-message-preview__footer" placeholder="Adicionar rodapé" />
          </div>
        </div>
        <ImageSlot urlField={thumbnailUrlField} modeField={thumbnailModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__thumbnail" alt="Thumbnail da mensagem" fallbackLabel="Adicionar thumbnail" />
      </div>
    </div>
  );
}


export function WelcomeDmEmbedPreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const titleField = findExact(fields, "welcome.dm.title");
  const bodyField = findExact(fields, "welcome.dm.body");
  const footerField = findExact(fields, "welcome.dm.footer");
  const accent = normalizedColor(draft["welcome.accent_color"]) ?? "#5865F2";
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };
  return (
    <div className="osk-message-preview__message">
      <div className="osk-message-preview__embed" style={{ "--osk-message-accent": accent } as CSSProperties}>
        <div className="osk-message-preview__embed-main">
          <FieldText {...textProps} field={titleField} className="osk-message-preview__title" placeholder="Adicionar título" />
          <FieldText {...textProps} field={bodyField} className="osk-message-preview__description" placeholder="Adicionar mensagem" />
          <div className="osk-message-preview__footer-row">
            <FieldText {...textProps} field={footerField} className="osk-message-preview__footer" placeholder="Adicionar rodapé" />
          </div>
        </div>
      </div>
    </div>
  );
}
