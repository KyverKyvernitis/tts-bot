import { Pencil } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useState } from "react";
import type {
  DashboardColorSlot,
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import { DiscordRichText } from "./DiscordRichText";
import { MessageInlineTextEditor } from "./MessageInlineTextEditor";
import {
  isValidPreviewUrl,
  readableFieldLabel,
} from "./messageEditorUtils";

interface MessagePreviewProps {
  sectionId?: string;
  editorId?: string;
  groupLabel: string;
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  selectedColorSlot?: number | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  onSelectColorSlot?(slotNumber: number): void;
}

function findField(
  fields: DashboardFieldDefinition[],
  suffixes: string[],
): DashboardFieldDefinition | undefined {
  return fields.find((item) => suffixes.some((suffix) => item.id.endsWith(suffix)));
}

function rawFieldValue(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): unknown {
  return field ? draft[field.id] : undefined;
}

function fieldString(
  field: DashboardFieldDefinition | undefined,
  draft: Record<string, unknown>,
): string {
  const value = rawFieldValue(field, draft);
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

function previewColor(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): string | null {
  const colorField = fields.find((field) => field.type === "color");
  const value = colorField ? draft[colorField.id] : null;
  if (typeof value !== "string") return null;
  const normalized = value.trim().startsWith("#") ? value.trim() : `#${value.trim()}`;
  return /^#[0-9a-fA-F]{6}$/.test(normalized) ? normalized : null;
}

function modeLabel(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): string | null {
  if (!field) return null;
  const value = fieldString(field, draft);
  if (!value || value === "none") return null;
  return field.options?.find((option) => option.value === value)?.label ?? readableFieldLabel(field);
}

function EditableRegion({
  field,
  interactive,
  selectedFieldId,
  onSelectField,
  onEditField,
  className,
  children,
  placeholder,
  textEditable = false,
}: {
  field?: DashboardFieldDefinition;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  className?: string;
  children?: ReactNode;
  placeholder?: string;
  textEditable?: boolean;
}) {
  if (!children && !interactive) return null;
  if (!interactive || !field || !onSelectField) {
    return <div className={className}>{children}</div>;
  }

  const selected = selectedFieldId === field.id;
  function selectOrEdit() {
    if (selected && textEditable && onEditField) onEditField(field!);
    else onSelectField!(field!);
  }

  return (
    <div
      role="button"
      tabIndex={0}
      className={className ? `osk-message-editable ${className}` : "osk-message-editable"}
      data-selected={selected || undefined}
      data-text-editable={textEditable || undefined}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        selectOrEdit();
      }}
      onDoubleClick={(event) => {
        if (!textEditable || !onEditField) return;
        event.preventDefault();
        event.stopPropagation();
        onSelectField(field);
        onEditField(field);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectOrEdit();
        }
      }}
      title={selected && textEditable ? `Editar ${field.label}` : `Selecionar ${field.label}`}
    >
      {children ?? <span className="osk-message-preview__ghost">+ {placeholder ?? readableFieldLabel(field)}</span>}
      {selected && textEditable && <span className="osk-message-editable__pencil" aria-hidden="true"><Pencil size={11} /></span>}
    </div>
  );
}

function FieldText({
  field,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  editingFieldId,
  textSelection,
  onSelectField,
  onEditField,
  onFinishEdit,
  onChange,
  onTextSelection,
  className,
  placeholder,
}: {
  field?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  className?: string;
  placeholder?: string;
}) {
  const value = fieldString(field, draft);
  const editing = Boolean(field && editingFieldId === field.id && onChange && onTextSelection && onFinishEdit);

  if (editing && field) {
    return (
      <div className={className ? `osk-message-editable osk-message-editable--editing ${className}` : "osk-message-editable osk-message-editable--editing"} data-selected="true">
        <MessageInlineTextEditor
          field={field}
          value={value}
          selection={textSelection?.fieldId === field.id ? textSelection : null}
          onChange={(next) => onChange!(field, next)}
          onSelection={(start, end) => onTextSelection!(field, start, end)}
          onFinish={onFinishEdit!}
        />
      </div>
    );
  }

  const content = value.trim()
    ? <DiscordRichText text={value} guildOptions={guildOptions} />
    : undefined;
  return (
    <EditableRegion
      field={field}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      onEditField={onEditField}
      className={className}
      placeholder={placeholder}
      textEditable={Boolean(field && (field.type === "text" || field.type === "textarea"))}
    >
      {content}
    </EditableRegion>
  );
}

function MessageImage({
  src,
  alt,
  className,
  placeholder,
}: {
  src: string;
  alt: string;
  className: string;
  placeholder: string;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  if (failed || !isValidPreviewUrl(src)) {
    return <div className={`${className} osk-message-preview__image-placeholder`}>{placeholder}</div>;
  }
  return (
    <img
      className={className}
      src={src}
      alt={alt}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}

function ImageSlot({
  urlField,
  modeField,
  draft,
  interactive,
  selectedFieldId,
  onSelectField,
  className,
  alt,
  fallbackLabel,
}: {
  urlField?: DashboardFieldDefinition;
  modeField?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  className: string;
  alt: string;
  fallbackLabel: string;
}) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const targetField = modeValue === "custom" && urlField ? urlField : modeField ?? urlField;
  const label = modeLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || interactive || Boolean(modeLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion
      field={targetField}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      className={`${className}-wrap`}
      placeholder={label}
    >
      {isValidPreviewUrl(url) ? (
        <MessageImage src={url} alt={alt} className={className} placeholder={label} />
      ) : (
        <span className="osk-message-preview__image-placeholder">{label}</span>
      )}
    </EditableRegion>
  );
}

function IconSlot({
  urlField,
  modeField,
  draft,
  interactive,
  selectedFieldId,
  onSelectField,
  alt,
  fallbackLabel,
}: {
  urlField?: DashboardFieldDefinition;
  modeField?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  alt: string;
  fallbackLabel: string;
}) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const targetField = modeValue === "custom" && urlField ? urlField : modeField ?? urlField;
  const label = modeLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || Boolean(modeLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion
      field={targetField}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      className="osk-message-preview__icon-wrap"
      placeholder={label}
    >
      {isValidPreviewUrl(url) ? (
        <MessageImage src={url} alt={alt} className="osk-message-preview__icon" placeholder={label} />
      ) : (
        <span className="osk-message-preview__icon-placeholder" title={label}>{label.slice(0, 1).toUpperCase()}</span>
      )}
    </EditableRegion>
  );
}

function EmbedPreview(props: Omit<MessagePreviewProps, "groupLabel" | "botName" | "botAvatarUrl" | "sectionId" | "editorId" | "selectedColorSlot" | "onSelectColorSlot">) {
  const {
    fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection,
    onSelectField, onEditField, onFinishEdit, onChange, onTextSelection,
  } = props;
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
  const colorTargetField = fieldString(colorModeField, draft) === "fixed" && colorField ? colorField : colorModeField ?? colorField;

  const hasEmbedContent = [authorField, titleField, descriptionField, footerField]
    .map((field) => fieldString(field, draft))
    .some((value) => value.trim())
    || isValidPreviewUrl(fieldString(imageUrlField, draft))
    || isValidPreviewUrl(fieldString(thumbnailUrlField, draft))
    || Boolean(modeLabel(imageModeField, draft))
    || Boolean(modeLabel(thumbnailModeField, draft))
    || Boolean(modeLabel(authorIconModeField, draft))
    || Boolean(modeLabel(footerIconModeField, draft));

  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-message-preview__message">
      <FieldText {...textProps} field={contentField} className="osk-message-preview__content" placeholder="Adicionar conteúdo" />
      {hasEmbedContent || interactive ? (
        <div className="osk-message-preview__embed" style={style}>
          {interactive && colorTargetField && (
            <button
              type="button"
              className="osk-message-preview__accent-control"
              data-selected={selectedFieldId === colorTargetField.id || undefined}
              aria-label="Editar cor do embed"
              onClick={(event) => {
                event.stopPropagation();
                onSelectField?.(colorTargetField);
              }}
            />
          )}
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
      ) : (
        <div className="osk-message-preview__placeholder">Adicione conteúdo ao embed para começar.</div>
      )}
    </div>
  );
}

function GenericMessagePreview(props: Omit<MessagePreviewProps, "groupLabel" | "botName" | "botAvatarUrl" | "sectionId" | "editorId" | "selectedColorSlot" | "onSelectColorSlot">) {
  const {
    fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection,
    onSelectField, onEditField, onFinishEdit, onChange, onTextSelection,
  } = props;
  const textFields = fields.filter((field) => {
    if (field.type !== "text" && field.type !== "textarea") return false;
    const hint = `${field.id} ${field.label}`.toLocaleLowerCase("pt-BR");
    return !/(button_label|button_emoji|button_style|approve_label|approve_emoji|approve_style|reject_label|reject_emoji|reject_style|placeholder|emoji do botão|cor do botão)/.test(hint);
  });
  const explicitMessageFields = textFields.filter((field) =>
    field.id.includes(".templates.")
    || field.id.includes(".public.")
    || field.id.includes(".dm."),
  );
  const previewFields = explicitMessageFields.length > 0 ? explicitMessageFields : textFields;
  const visible = previewFields
    .map((field) => ({ field, value: typeof draft[field.id] === "string" ? String(draft[field.id]) : "" }))
    .filter(({ value }) => value.trim() || interactive);
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;
  const imageFields = fields.filter((field) => field.type === "url" && /(image|imagem|media|mídia|banner)/i.test(`${field.id} ${field.label}`));
  const colorField = fields.find((field) => field.type === "color");
  const buttonLabelField = fields.find((field) => /(button_label|approve_label|reject_label)/i.test(field.id));
  const buttonEmojiField = fields.find((field) => /(button_emoji|approve_emoji|reject_emoji)/i.test(field.id));
  const buttonStyleField = fields.find((field) => /(button_style|approve_style|reject_style)/i.test(field.id));
  const placeholderField = fields.find((field) => /placeholder/i.test(field.id));
  const imageEntries = imageFields.map((field) => ({ field, url: fieldString(field, draft) }));
  const buttonLabel = fieldString(buttonLabelField, draft);
  const buttonEmoji = fieldString(buttonEmojiField, draft);
  const buttonStyle = fieldString(buttonStyleField, draft) || "primary";
  const placeholder = fieldString(placeholderField, draft);
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, onFinishEdit, onChange, onTextSelection };

  if (visible.length === 0 && !imageEntries.some(({ url }) => isValidPreviewUrl(url)) && !buttonLabel && !placeholder && !interactive) {
    return <div className="osk-message-preview__placeholder">Adicione conteúdo para começar.</div>;
  }

  const shouldSeparate = visible.length > 1 && visible.every(({ field }) => field.id.includes(".templates.") || field.id.includes(".texts."));
  if (shouldSeparate) {
    return (
      <div className="osk-message-preview__templates">
        {visible.map(({ field, value }) => (
          <div key={field.id} className="osk-message-preview__template-block">
            <span>{readableFieldLabel(field)}</span>
            <FieldText {...textProps} field={field} className="osk-message-preview__template" placeholder="Mensagem" />
            {!value.trim() && !interactive && <p className="osk-message-preview__ghost">Sem conteúdo</p>}
          </div>
        ))}
      </div>
    );
  }

  const titleEntry = visible.find(({ field }) => /(?:^|\.)(title)$/.test(field.id) || /título/i.test(field.label));
  const footerEntry = visible.find(({ field }) => /(?:^|\.)(footer|footer_text)$/.test(field.id) || /rodapé/i.test(field.label));
  const bodyEntries = visible.filter((entry) => entry !== titleEntry && entry !== footerEntry);

  return (
    <div className="osk-message-preview__message-card" style={style}>
      {interactive && colorField && <button type="button" className="osk-message-preview__accent-control" data-selected={selectedFieldId === colorField.id || undefined} aria-label="Editar cor da mensagem" onClick={(event) => { event.stopPropagation(); onSelectField?.(colorField); }} />}
      {titleEntry && <FieldText {...textProps} field={titleEntry.field} className="osk-message-preview__card-title" placeholder="Adicionar título" />}
      {bodyEntries.map(({ field }) => (
        <div key={field.id} className="osk-message-preview__body-wrap">
          {bodyEntries.length > 1 && <small>{readableFieldLabel(field)}</small>}
          <FieldText {...textProps} field={field} className="osk-message-preview__body" placeholder="Mensagem" />
        </div>
      ))}
      {imageEntries.length > 0 && <div className="osk-message-preview__generic-media">
        {imageEntries.map(({ field, url }, index) => {
          const side = /(side|lateral|thumbnail)/i.test(`${field.id} ${field.label}`);
          if (!isValidPreviewUrl(url) && !interactive) return null;
          return <EditableRegion key={field.id} field={field} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className={`osk-message-preview__generic-image-wrap${side ? " osk-message-preview__generic-image-wrap--side" : ""}`} placeholder={field.label || `Imagem ${index + 1}`}>
            {isValidPreviewUrl(url) ? <MessageImage src={url} alt={field.label || "Imagem da mensagem"} className="osk-message-preview__generic-image" placeholder="Imagem indisponível" /> : <span className="osk-message-preview__image-placeholder">{field.label || "Imagem"}</span>}
          </EditableRegion>;
        })}
      </div>}
      {(placeholder || (interactive && placeholderField)) && (
        <EditableRegion field={placeholderField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Menu de seleção">
          <div className="osk-message-preview__select-sim"><span>{placeholder || "Placeholder do seletor"}</span><span>⌄</span></div>
        </EditableRegion>
      )}
      {(buttonLabel || (interactive && buttonLabelField)) && (
        <EditableRegion field={buttonLabelField ?? buttonStyleField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Botão">
          <div className="osk-message-preview__button-row"><span data-style={buttonStyle}>{buttonEmoji && <><DiscordRichText text={buttonEmoji} guildOptions={guildOptions} compact /> </>}<DiscordRichText text={buttonLabel || "Texto do botão"} guildOptions={guildOptions} compact /></span></div>
        </EditableRegion>
      )}
      {footerEntry && <FieldText {...textProps} field={footerEntry.field} className="osk-message-preview__card-footer" placeholder="Adicionar rodapé" />}
    </div>
  );
}

function ColorRolesPanelPreview({
  editorId,
  fields,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  editingFieldId,
  selectedColorSlot,
  textSelection,
  onSelectField,
  onEditField,
  onFinishEdit,
  onChange,
  onTextSelection,
  onSelectColorSlot,
}: Omit<MessagePreviewProps, "groupLabel" | "botName" | "botAvatarUrl" | "sectionId">) {
  const panelNumber = Math.max(1, Math.min(5, Number(editorId?.match(/color-panel-(\d+)/)?.[1] || 1)));
  const titleField = fields.find((field) => field.id === `color_roles.messages.${panelNumber}.title`);
  const subtitleField = fields.find((field) => field.id === `color_roles.messages.${panelNumber}.subtitle`);
  const footerField = fields.find((field) => field.id === `color_roles.messages.${panelNumber}.footer`);
  const slotsField = fields.find((field) => field.id === "color_roles.slots");
  const rawSlots = draft["color_roles.slots"];
  const slots = rawSlots && typeof rawSlots === "object" ? rawSlots as Record<string, DashboardColorSlot> : {};
  const start = (panelNumber - 1) * 10 + 1;
  const panelSlots = Array.from({ length: 10 }, (_, index) => {
    const number = start + index;
    const value = slots[String(number)] || ({ number, name: `Cor ${number}`, text_hex: "#ffffff", role_hex: "#ffffff" } as DashboardColorSlot);
    return { ...value, number };
  });
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-color-panel-canvas" data-panel={panelNumber}>
      <div className="osk-color-panel-canvas__copy">
        <FieldText {...textProps} field={titleField} className="osk-color-panel-canvas__title" placeholder="Título do painel" />
        <FieldText {...textProps} field={subtitleField} className="osk-color-panel-canvas__subtitle" placeholder="Subtítulo" />
        <FieldText {...textProps} field={footerField} className="osk-color-panel-canvas__footer" placeholder="Adicionar rodapé" />
      </div>
      {panelNumber <= 3 && (
        <>
          <div className="osk-color-panel-canvas__image" aria-label={`Cores ${start} a ${start + 9}`}>
            {panelSlots.map((slot) => {
              const color = /^#[0-9a-f]{6}$/i.test(String(slot.text_hex || "")) ? String(slot.text_hex) : "#ffffff";
              return (
                <button
                  type="button"
                  key={slot.number}
                  className="osk-color-panel-canvas__slot"
                  data-selected={selectedColorSlot === slot.number || undefined}
                  style={{ "--osk-slot-color": color } as CSSProperties}
                  disabled={!interactive || !slotsField}
                  onClick={(event) => {
                    event.stopPropagation();
                    if (!slotsField) return;
                    onSelectField?.(slotsField);
                    onSelectColorSlot?.(slot.number);
                  }}
                >
                  <b>{slot.number}.</b><span>{String(slot.name || `Cor ${slot.number}`)}</span>
                </button>
              );
            })}
          </div>
          <div className="osk-message-preview__select-sim osk-color-panel-canvas__select"><span>Selecione uma cor</span><span>⌄</span></div>
        </>
      )}
    </div>
  );
}

export function MessagePreview({
  sectionId,
  editorId,
  groupLabel,
  fields,
  draft,
  guildOptions,
  botName = "Osaka",
  botAvatarUrl,
  interactive,
  selectedFieldId,
  editingFieldId,
  selectedColorSlot,
  textSelection,
  onSelectField,
  onEditField,
  onFinishEdit,
  onChange,
  onTextSelection,
  onSelectColorSlot,
}: MessagePreviewProps) {
  const hasEmbedFields = fields.some((field) => field.id.includes(".embed."));
  const hasPublicFields = fields.some((field) => field.id.includes(".public."));
  const welcomeMode = String(draft["welcome.render_mode"] || "");
  const isEmbed = hasEmbedFields && (!hasPublicFields || welcomeMode === "embed");
  const previewFields = hasPublicFields && welcomeMode !== "embed" ? fields.filter((field) => !field.id.includes(".embed.")) : fields;
  const isColorPanel = sectionId === "color_roles" && /^color-panel-\d+$/.test(editorId || "");
  const shared = {
    fields: previewFields,
    draft,
    guildOptions,
    interactive,
    selectedFieldId,
    editingFieldId,
    selectedColorSlot,
    textSelection,
    onSelectField,
    onEditField,
    onFinishEdit,
    onChange,
    onTextSelection,
    onSelectColorSlot,
  };

  return (
    <div className="osk-message-preview" data-interactive={interactive ? "true" : "false"} data-editor-kind={isColorPanel ? "color-panel" : isEmbed ? "embed" : "generic"}>
      <div className="osk-message-preview__header">
        <SmartAvatar name={botName} src={botAvatarUrl} type="server" size={34} className="osk-message-preview__avatar" />
        <div><strong>{botName}</strong><span>BOT</span></div>
      </div>
      <div className="osk-message-preview__canvas" aria-label={`Mensagem editável de ${groupLabel}`}>
        {isColorPanel ? (
          <ColorRolesPanelPreview {...shared} editorId={editorId} />
        ) : isEmbed ? (
          <EmbedPreview {...shared} />
        ) : (
          <GenericMessagePreview {...shared} />
        )}
      </div>
    </div>
  );
}
