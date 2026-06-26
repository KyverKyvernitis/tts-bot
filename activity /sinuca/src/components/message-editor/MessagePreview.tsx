import type { CSSProperties } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import {
  isValidPreviewUrl,
  readableFieldLabel,
} from "./messageEditorUtils";

interface MessagePreviewProps {
  groupLabel: string;
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
}

function fieldValue(
  fields: DashboardFieldDefinition[],
  draft: Record<string, unknown>,
  suffix: string,
): string {
  const field = fields.find((item) => item.id.endsWith(suffix));
  const value = field ? draft[field.id] : "";
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

function previewColor(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): string | null {
  const colorField = fields.find((field) => field.type === "color");
  const value = colorField ? draft[colorField.id] : null;
  if (typeof value !== "string") return null;
  const normalized = value.trim().startsWith("#") ? value.trim() : `#${value.trim()}`;
  return /^#[0-9a-fA-F]{6}$/.test(normalized) ? normalized : null;
}

function MessageText({ children, className }: { children: string; className?: string }) {
  if (!children.trim()) return null;
  return <div className={className}>{children}</div>;
}

function EmbedPreview({
  fields,
  draft,
}: Omit<MessagePreviewProps, "groupLabel">) {
  const content = fieldValue(fields, draft, ".embed.content");
  const author = fieldValue(fields, draft, ".embed.author_name");
  const title = fieldValue(fields, draft, ".embed.title");
  const description = fieldValue(fields, draft, ".embed.description");
  const footer = fieldValue(fields, draft, ".embed.footer_text");
  const imageUrl = fieldValue(fields, draft, ".embed.image_url");
  const thumbnailUrl = fieldValue(fields, draft, ".embed.thumbnail_url");
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;

  const hasEmbedContent = [author, title, description, footer].some((value) => value.trim())
    || isValidPreviewUrl(imageUrl)
    || isValidPreviewUrl(thumbnailUrl);

  return (
    <div className="osk-message-preview__message">
      <MessageText className="osk-message-preview__content">{content}</MessageText>
      {hasEmbedContent ? (
        <div className="osk-message-preview__embed" style={style}>
          <div className="osk-message-preview__embed-main">
            <MessageText className="osk-message-preview__author">{author}</MessageText>
            <MessageText className="osk-message-preview__title">{title}</MessageText>
            <MessageText className="osk-message-preview__description">{description}</MessageText>
            {isValidPreviewUrl(imageUrl) && (
              <img className="osk-message-preview__image" src={imageUrl} alt="Imagem da mensagem" />
            )}
            <MessageText className="osk-message-preview__footer">{footer}</MessageText>
          </div>
          {isValidPreviewUrl(thumbnailUrl) && (
            <img className="osk-message-preview__thumbnail" src={thumbnailUrl} alt="Thumbnail da mensagem" />
          )}
        </div>
      ) : (
        <div className="osk-message-preview__placeholder">Preencha os campos do embed para visualizar a mensagem.</div>
      )}
    </div>
  );
}

function GenericMessagePreview({ fields, draft }: Omit<MessagePreviewProps, "groupLabel">) {
  const textFields = fields.filter((field) => field.type === "text" || field.type === "textarea");
  const explicitMessageFields = textFields.filter((field) =>
    field.id.includes(".templates.")
    || field.id.includes(".public.")
    || field.id.includes(".dm."),
  );
  const previewFields = explicitMessageFields.length > 0 ? explicitMessageFields : textFields;
  const visible = previewFields
    .map((field) => ({ field, value: typeof draft[field.id] === "string" ? String(draft[field.id]) : "" }))
    .filter(({ value }) => value.trim());
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;

  if (visible.length === 0) {
    return <div className="osk-message-preview__placeholder">Preencha uma mensagem para visualizar o resultado.</div>;
  }

  const titleEntry = visible.find(({ field }) => /(?:^|\.)(title)$/.test(field.id) || /título/i.test(field.label));
  const footerEntry = visible.find(({ field }) => /(?:^|\.)(footer|footer_text)$/.test(field.id) || /rodapé/i.test(field.label));
  const bodyEntries = visible.filter((entry) => entry !== titleEntry && entry !== footerEntry);

  const shouldSeparate = bodyEntries.length > 1 && fields.some((field) => field.id.startsWith("birthday.templates."));
  if (shouldSeparate) {
    return (
      <div className="osk-message-preview__templates">
        {visible.map(({ field, value }) => (
          <article key={field.id} className="osk-message-preview__template" style={style}>
            <span>{readableFieldLabel(field)}</span>
            <p>{value}</p>
          </article>
        ))}
      </div>
    );
  }

  return (
    <div className="osk-message-preview__message-card" style={style}>
      {titleEntry && <h3>{titleEntry.value}</h3>}
      {bodyEntries.map(({ field, value }) => (
        <div key={field.id} className="osk-message-preview__body">
          {bodyEntries.length > 1 && <small>{readableFieldLabel(field)}</small>}
          <p>{value}</p>
        </div>
      ))}
      {footerEntry && <footer>{footerEntry.value}</footer>}
    </div>
  );
}

export function MessagePreview({ groupLabel, fields, draft }: MessagePreviewProps) {
  const isEmbed = fields.some((field) => field.id.includes(".embed."));
  return (
    <div className="osk-message-preview">
      <div className="osk-message-preview__header">
        <SmartAvatar
          name="Core"
          type="server"
          size={34}
          className="osk-message-preview__avatar"
        />
        <div>
          <strong>Core</strong>
          <span>BOT</span>
        </div>
      </div>
      <div className="osk-message-preview__canvas" aria-label={`Prévia de ${groupLabel}`}>
        {isEmbed ? (
          <EmbedPreview fields={fields} draft={draft} />
        ) : (
          <GenericMessagePreview fields={fields} draft={draft} />
        )}
      </div>
    </div>
  );
}
