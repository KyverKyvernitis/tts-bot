import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { discordAttachmentUrlInfo, isValidPreviewUrl } from "./messageEditorUtils";
import { MessageContextualSelect } from "./MessageContextualSelect";
import {
  contextualMessageOptions,
  isMessagePreviewImageUrlField,
  messageVisualValuesEqual,
} from "./messageVisualEditorModel";

interface MessageVisualEditorProps {
  fields: DashboardFieldDefinition[];
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onFocusField?(field: DashboardFieldDefinition): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  selectedFieldId?: string | null;
  selectedColorSlot?: number | null;
  colorSlotIds?: number[] | null;
  onColorSlotSelect?(slotNumber: number): void;
  contextual?: boolean;
}

export function MessageVisualEditor({
  fields,
  baseline,
  draft,
  guildOptions,
  onChange,
  onFocusField,
  onTextSelection,
  selectedFieldId,
  selectedColorSlot,
  colorSlotIds,
  onColorSlotSelect,
  contextual = false,
}: MessageVisualEditorProps) {
  if (!fields.length) {
    return <div className="osk-message-empty">Nenhum campo está disponível nesta área.</div>;
  }

  return <div className="osk-message-form" data-contextual={contextual || undefined}>
    {fields.map((field) => {
      const changed = !messageVisualValuesEqual(baseline[field.id], draft[field.id]);
      const currentText = typeof draft[field.id] === "string" ? String(draft[field.id]) : "";
      const contextualOptions = contextualMessageOptions(field, currentText);
      const imageUrlField = isMessagePreviewImageUrlField(field);
      const validImageUrl = !currentText || isValidPreviewUrl(currentText);
      const discordAttachment = imageUrlField ? discordAttachmentUrlInfo(currentText) : null;
      return <section key={field.id} className="osk-message-form__field" data-changed={changed || undefined} data-selected={selectedFieldId === field.id || undefined} data-type={field.type} data-has-description={field.description ? "true" : undefined} onFocusCapture={() => onFocusField?.(field)}>
        <header>
          <div><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div>
          {field.maxLength && ["text", "textarea", "url"].includes(field.type) && <span>{currentText.length}/{field.maxLength}</span>}
        </header>
        {contextual && field.type === "select" && contextualOptions.length > 0 && contextualOptions.length <= 8 ? (
          <MessageContextualSelect
            field={field}
            options={contextualOptions}
            value={String(draft[field.id] ?? "")}
            onChange={(value) => onChange(field, value)}
          />
        ) : (
          <DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} onTextSelection={onTextSelection} selectedColorSlot={selectedColorSlot} colorSlotIds={colorSlotIds} onColorSlotSelect={onColorSlotSelect} />
        )}
        {imageUrlField && currentText && !validImageUrl ? (
          <small className="osk-message-url-hint" data-state="error">Cole um link HTTPS completo para carregar a imagem.</small>
        ) : null}
        {discordAttachment?.expired ? (
          <small className="osk-message-url-hint" data-state="error">Este link temporário do Discord expirou. Copie um link novo ou use uma hospedagem permanente.</small>
        ) : discordAttachment ? (
          <small className="osk-message-url-hint" data-state="warning">Links de anexos do Discord expiram. A prévia usa uma rota de compatibilidade, mas vale trocar por um link permanente.</small>
        ) : null}
      </section>;
    })}
  </div>;
}
