import { Pencil } from "lucide-react";
import { useMemo } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import {
  filterPreviewFields,
  resolvePreviewRenderPlan,
  resolveSender,
} from "./messagePreviewModel";
import type { MessagePreviewProps, PreviewCoreProps } from "./messagePreviewTypes";
import {
  ColorRolesPanelPreview,
  ComponentsV2Preview,
  EmbedPreview,
  GenericMessagePreview,
  WelcomeComponentsV2Preview,
  WelcomeDmEmbedPreview,
} from "./MessagePreviewVariants";

function SenderHeader({
  senderFields,
  draft,
  botName,
  botAvatarUrl,
  guildName,
  guildAvatarUrl,
  interactive,
  selected,
  onSelect,
  onEdit,
  fieldId,
}: {
  senderFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  botName: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
  interactive?: boolean;
  selected?: boolean;
  onSelect?(): void;
  onEdit?(): void;
  fieldId?: string;
}) {
  const sender = useMemo(() => resolveSender({ senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl }), [senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl]);
  const enabled = interactive && senderFields.length > 0 && onSelect;
  const activate = () => { if (selected && onEdit) onEdit(); else onSelect?.(); };
  const content = <>
    <SmartAvatar name={sender.name} src={sender.avatar} type="server" size={34} className="osk-message-preview__avatar" />
    <div><strong>{sender.name}</strong><span>{sender.badge}</span></div>
    {selected && <span className="osk-message-sender__pencil" aria-hidden="true"><Pencil size={12} /></span>}
  </>;
  if (!enabled) return <div className="osk-message-preview__header">{content}</div>;
  return (
    <div role="button" tabIndex={0} className="osk-message-preview__header osk-message-sender" data-selected={selected || undefined} data-webhook={sender.enabled || undefined} data-message-field-anchor={fieldId} onClick={(event) => { event.stopPropagation(); activate(); }} onDoubleClick={(event) => { event.stopPropagation(); onSelect?.(); onEdit?.(); }} onKeyDown={(event) => { if (event.key !== "Enter" && event.key !== " ") return; event.preventDefault(); activate(); }} aria-label="Editar remetente da mensagem" title="Configurar remetente">
      {content}
    </div>
  );
}

export function MessagePreview({
  sectionId,
  editorId,
  groupLabel,
  presentation = "generic",
  fields,
  senderFields = [],
  draft,
  guildOptions,
  botName = "Osaka",
  botAvatarUrl,
  guildName,
  guildAvatarUrl,
  interactive,
  senderSelected,
  selectedFieldId,
  editingFieldId,
  selectedColorSlot,
  textSelection,
  onSelectSender,
  onEditSender,
  onSelectField,
  onEditField,
  hasFieldOptions,
  onOpenFieldOptions,
  onFinishEdit,
  onChange,
  onTextSelection,
  onSelectColorSlot,
}: MessagePreviewProps) {
  const { isDm, welcomeMode, adaptiveWelcome, renderKind } = resolvePreviewRenderPlan(presentation, sectionId, editorId, draft);
  const previewFields = filterPreviewFields(fields, { adaptiveWelcome, isDm, welcomeMode });
  const shared: PreviewCoreProps = {
    sectionId,
    editorId,
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
    hasFieldOptions,
    onOpenFieldOptions,
    onFinishEdit,
    onChange,
    onTextSelection,
    onSelectColorSlot,
  };

  return (
    <div className="osk-message-preview" data-interactive={interactive ? "true" : "false"} data-editor-kind={renderKind}>
      <SenderHeader senderFields={senderFields} draft={draft} botName={botName} botAvatarUrl={botAvatarUrl} guildName={guildName} guildAvatarUrl={guildAvatarUrl} interactive={interactive} selected={senderSelected} onSelect={onSelectSender} onEdit={onEditSender} fieldId={senderFields.find((field) => field.id === "welcome.webhook.enabled")?.id ?? senderFields[0]?.id} />
      <div className="osk-message-preview__canvas" aria-label={`Mensagem editável de ${groupLabel}`}>
        {renderKind === "color-panel" ? <ColorRolesPanelPreview {...shared} />
          : adaptiveWelcome && welcomeMode === "components_v2" ? <WelcomeComponentsV2Preview {...shared} dm={isDm} />
            : renderKind === "components-v2" ? <ComponentsV2Preview {...shared} />
              : renderKind === "embed" && isDm ? <WelcomeDmEmbedPreview {...shared} />
                : renderKind === "embed" ? <EmbedPreview {...shared} />
                  : <GenericMessagePreview {...shared} />}
      </div>
    </div>
  );
}
