import { useEffect, useMemo, useState } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { discordAttachmentUrlInfo, isValidPreviewUrl, normalizePreviewUrl, previewImageCandidates } from "./messageEditorUtils";
import { fieldString, optionLabel } from "./messagePreviewModel";
import { EditableRegion } from "./MessagePreviewEditablePrimitives";

export function MessageImage({ src, alt, className, placeholder }: { src: string; alt: string; className: string; placeholder: string }) {
  const normalizedSrc = normalizePreviewUrl(src);
  const candidates = useMemo(() => previewImageCandidates(normalizedSrc), [normalizedSrc]);
  const discordInfo = useMemo(() => discordAttachmentUrlInfo(normalizedSrc), [normalizedSrc]);
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    setCandidateIndex(0);
    setFailed(false);
    setLoaded(false);
  }, [normalizedSrc]);

  const currentSrc = candidates[candidateIndex] || "";
  if (failed || !currentSrc) {
    const errorLabel = discordInfo?.expired ? "Link do Discord expirado" : "Imagem indisponível";
    return <div className={`${className} osk-message-preview__image-placeholder`} data-error={failed || undefined}>{failed ? errorLabel : placeholder}</div>;
  }

  const handleError = () => {
    if (candidateIndex + 1 < candidates.length) {
      setCandidateIndex((current) => current + 1);
      setLoaded(false);
      return;
    }
    setFailed(true);
  };

  return <span className={`osk-message-image-loader ${className}-loader`} data-loaded={loaded || undefined}>
    {!loaded && <span className={`${className} osk-message-preview__image-placeholder`}>Carregando imagem…</span>}
    <img
      key={currentSrc}
      className={className}
      src={currentSrc}
      alt={alt}
      loading="eager"
      decoding="async"
      referrerPolicy="no-referrer"
      onLoad={() => setLoaded(true)}
      onError={handleError}
    />
  </span>;
}

export function ImageSlot({
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
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || interactive || Boolean(optionLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className={`${className}-wrap`} placeholder={label}>
      {isValidPreviewUrl(url)
        ? <MessageImage src={url} alt={alt} className={className} placeholder={label} />
        : <span className="osk-message-preview__image-placeholder">{label}</span>}
    </EditableRegion>
  );
}

export function IconSlot({
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
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || Boolean(optionLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__icon-wrap" placeholder={label}>
      {isValidPreviewUrl(url)
        ? <MessageImage src={url} alt={alt} className="osk-message-preview__icon" placeholder={label} />
        : <span className="osk-message-preview__icon-placeholder" title={label}>{label.slice(0, 1).toUpperCase()}</span>}
    </EditableRegion>
  );
}
