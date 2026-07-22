import type { CSSProperties, ReactNode } from "react";
import { useState } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import { DiscordRichText } from "./DiscordRichText";
import {
  isValidPreviewUrl,
  readableFieldLabel,
} from "./messageEditorUtils";

interface MessagePreviewProps {
  groupLabel: string;
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
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
  className,
  children,
  placeholder,
}: {
  field?: DashboardFieldDefinition;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  className?: string;
  children?: ReactNode;
  placeholder?: string;
}) {
  if (!children && !interactive) return null;
  if (!interactive || !field || !onSelectField) {
    return <div className={className}>{children}</div>;
  }
  const editableField = field;
  const selectField = onSelectField;
  function select() {
    selectField(editableField);
  }

  return (
    <div
      role="button"
      tabIndex={0}
      className={className ? `osk-message-editable ${className}` : "osk-message-editable"}
      data-selected={selectedFieldId === field.id}
      onClick={(event) => {
        event.stopPropagation();
        select();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          select();
        }
      }}
      title={`Editar ${field.label}`}
    >
      {children ?? <span className="osk-message-preview__ghost">{placeholder ?? readableFieldLabel(field)}</span>}
    </div>
  );
}

function FieldText({
  field,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  onSelectField,
  className,
  placeholder,
}: {
  field?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  className?: string;
  placeholder?: string;
}) {
  const value = fieldString(field, draft);
  const content = value.trim()
    ? <DiscordRichText text={value} guildOptions={guildOptions} />
    : undefined;
  return (
    <EditableRegion
      field={field}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      className={className}
      placeholder={placeholder}
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
  const targetField = urlField ?? modeField;
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

function EmbedPreview({
  fields,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  onSelectField,
}: Omit<MessagePreviewProps, "groupLabel" | "botName" | "botAvatarUrl">) {
  const contentField = findField(fields, [".embed.content"]);
  const authorField = findField(fields, [".embed.author_name"]);
  const titleField = findField(fields, [".embed.title"]);
  const descriptionField = findField(fields, [".embed.description"]);
  const footerField = findField(fields, [".embed.footer_text"]);
  const imageUrlField = findField(fields, [".embed.image_url"]);
  const imageModeField = findField(fields, [".embed.image_mode"]);
  const thumbnailUrlField = findField(fields, [".embed.thumbnail_url"]);
  const thumbnailModeField = findField(fields, [".embed.thumbnail_mode"]);
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;

  const hasEmbedContent = [authorField, titleField, descriptionField, footerField]
    .map((field) => fieldString(field, draft))
    .some((value) => value.trim())
    || isValidPreviewUrl(fieldString(imageUrlField, draft))
    || isValidPreviewUrl(fieldString(thumbnailUrlField, draft))
    || Boolean(modeLabel(imageModeField, draft))
    || Boolean(modeLabel(thumbnailModeField, draft));

  return (
    <div className="osk-message-preview__message">
      <FieldText
        field={contentField}
        draft={draft}
        guildOptions={guildOptions}
        interactive={interactive}
        selectedFieldId={selectedFieldId}
        onSelectField={onSelectField}
        className="osk-message-preview__content"
        placeholder="Texto acima do embed"
      />
      {hasEmbedContent || interactive ? (
        <div className="osk-message-preview__embed" style={style}>
          <div className="osk-message-preview__embed-main">
            <FieldText
              field={authorField}
              draft={draft}
              guildOptions={guildOptions}
              interactive={interactive}
              selectedFieldId={selectedFieldId}
              onSelectField={onSelectField}
              className="osk-message-preview__author"
              placeholder="Autor"
            />
            <FieldText
              field={titleField}
              draft={draft}
              guildOptions={guildOptions}
              interactive={interactive}
              selectedFieldId={selectedFieldId}
              onSelectField={onSelectField}
              className="osk-message-preview__title"
              placeholder="Título"
            />
            <FieldText
              field={descriptionField}
              draft={draft}
              guildOptions={guildOptions}
              interactive={interactive}
              selectedFieldId={selectedFieldId}
              onSelectField={onSelectField}
              className="osk-message-preview__description"
              placeholder="Descrição"
            />
            <ImageSlot
              urlField={imageUrlField}
              modeField={imageModeField}
              draft={draft}
              interactive={interactive}
              selectedFieldId={selectedFieldId}
              onSelectField={onSelectField}
              className="osk-message-preview__image"
              alt="Imagem da mensagem"
              fallbackLabel="Imagem principal"
            />
            <FieldText
              field={footerField}
              draft={draft}
              guildOptions={guildOptions}
              interactive={interactive}
              selectedFieldId={selectedFieldId}
              onSelectField={onSelectField}
              className="osk-message-preview__footer"
              placeholder="Rodapé"
            />
          </div>
          <ImageSlot
            urlField={thumbnailUrlField}
            modeField={thumbnailModeField}
            draft={draft}
            interactive={interactive}
            selectedFieldId={selectedFieldId}
            onSelectField={onSelectField}
            className="osk-message-preview__thumbnail"
            alt="Thumbnail da mensagem"
            fallbackLabel="Thumbnail"
          />
        </div>
      ) : (
        <div className="osk-message-preview__placeholder">Preencha os campos do embed para visualizar a mensagem.</div>
      )}
    </div>
  );
}

function GenericMessagePreview({
  fields,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  onSelectField,
}: Omit<MessagePreviewProps, "groupLabel" | "botName" | "botAvatarUrl">) {
  const textFields = fields.filter((field) => field.type === "text" || field.type === "textarea");
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

  if (visible.length === 0) {
    return <div className="osk-message-preview__placeholder">Preencha uma mensagem para visualizar o resultado.</div>;
  }

  const shouldSeparate = visible.length > 1 && fields.some((field) => field.id.startsWith("birthday.templates."));
  if (shouldSeparate) {
    return (
      <div className="osk-message-preview__templates">
        {visible.map(({ field, value }) => (
          <EditableRegion
            key={field.id}
            field={field}
            interactive={interactive}
            selectedFieldId={selectedFieldId}
            onSelectField={onSelectField}
            className="osk-message-preview__template"
          >
            <span>{readableFieldLabel(field)}</span>
            {value.trim() ? <p><DiscordRichText text={value} guildOptions={guildOptions} /></p> : <p className="osk-message-preview__ghost">Clique para editar</p>}
          </EditableRegion>
        ))}
      </div>
    );
  }

  const titleEntry = visible.find(({ field }) => /(?:^|\.)(title)$/.test(field.id) || /título/i.test(field.label));
  const footerEntry = visible.find(({ field }) => /(?:^|\.)(footer|footer_text)$/.test(field.id) || /rodapé/i.test(field.label));
  const bodyEntries = visible.filter((entry) => entry !== titleEntry && entry !== footerEntry);

  return (
    <div className="osk-message-preview__message-card" style={style}>
      {titleEntry && (
        <EditableRegion
          field={titleEntry.field}
          interactive={interactive}
          selectedFieldId={selectedFieldId}
          onSelectField={onSelectField}
          className="osk-message-preview__card-title"
        >
          <h3>{titleEntry.value ? <DiscordRichText text={titleEntry.value} guildOptions={guildOptions} /> : "Título"}</h3>
        </EditableRegion>
      )}
      {bodyEntries.map(({ field, value }) => (
        <EditableRegion
          key={field.id}
          field={field}
          interactive={interactive}
          selectedFieldId={selectedFieldId}
          onSelectField={onSelectField}
          className="osk-message-preview__body"
        >
          {bodyEntries.length > 1 && <small>{readableFieldLabel(field)}</small>}
          <p>{value ? <DiscordRichText text={value} guildOptions={guildOptions} /> : <span className="osk-message-preview__ghost">Clique para editar</span>}</p>
        </EditableRegion>
      ))}
      {footerEntry && (
        <EditableRegion
          field={footerEntry.field}
          interactive={interactive}
          selectedFieldId={selectedFieldId}
          onSelectField={onSelectField}
          className="osk-message-preview__card-footer"
        >
          {footerEntry.value ? <DiscordRichText text={footerEntry.value} guildOptions={guildOptions} /> : "Rodapé"}
        </EditableRegion>
      )}
    </div>
  );
}

export function MessagePreview({
  groupLabel,
  fields,
  draft,
  guildOptions,
  botName = "Core",
  botAvatarUrl,
  interactive,
  selectedFieldId,
  onSelectField,
}: MessagePreviewProps) {
  const isEmbed = fields.some((field) => field.id.includes(".embed."));
  return (
    <div className="osk-message-preview" data-interactive={interactive ? "true" : "false"}>
      <div className="osk-message-preview__header">
        <SmartAvatar
          name={botName}
          src={botAvatarUrl}
          type="server"
          size={34}
          className="osk-message-preview__avatar"
        />
        <div>
          <strong>{botName}</strong>
          <span>BOT</span>
        </div>
      </div>
      <div className="osk-message-preview__canvas" aria-label={`Prévia de ${groupLabel}`}>
        {isEmbed ? (
          <EmbedPreview
            fields={fields}
            draft={draft}
            guildOptions={guildOptions}
            interactive={interactive}
            selectedFieldId={selectedFieldId}
            onSelectField={onSelectField}
          />
        ) : (
          <GenericMessagePreview
            fields={fields}
            draft={draft}
            guildOptions={guildOptions}
            interactive={interactive}
            selectedFieldId={selectedFieldId}
            onSelectField={onSelectField}
          />
        )}
      </div>
    </div>
  );
}
