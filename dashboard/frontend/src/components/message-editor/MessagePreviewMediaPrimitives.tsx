import { useContext, useEffect, useMemo, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import { discordAttachmentUrlInfo, isValidPreviewUrl, normalizePreviewUrl, previewImageCandidates } from "./messageEditorUtils";
import { fieldString, optionLabel } from "./messagePreviewModel";
import { MessageMediaContext } from "./messagePreviewMediaContext";
import { EditableRegion } from "./MessagePreviewEditablePrimitives";

interface MessageImageProps {
  src: string;
  alt: string;
  className: string;
  placeholder: string;
  onFailureChange?(failed: boolean): void;
}

export function MessageImage({ src, alt, className, placeholder, onFailureChange }: MessageImageProps) {
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
  useEffect(() => { onFailureChange?.(failed); }, [failed, onFailureChange]);

  const currentSrc = candidates[candidateIndex] || "";
  if (failed || !currentSrc) {
    const errorLabel = discordInfo?.expired ? "Link do Discord expirado" : "Imagem indisponível";
    return <span className={`${className} osk-message-preview__image-placeholder`} data-error={failed || undefined}>
      <span>{failed ? errorLabel : placeholder}</span>
      {failed && onFailureChange && <span className="osk-media-replace-label" aria-hidden="true">Trocar imagem <ArrowUpRight size={13} /></span>}
    </span>;
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
    <img key={currentSrc} className={className} src={currentSrc} alt={alt} loading="eager" decoding="async" referrerPolicy="no-referrer"
      onLoad={() => setLoaded(true)} onError={handleError} />
  </span>;
}

function DynamicMedia({ mode, label, className, icon = false }: { mode: string; label: string; className: string; icon?: boolean }) {
  const identity = useContext(MessageMediaContext);
  if (mode === "server" || mode === "bot") {
    const server = mode === "server";
    return <span className={`${className} osk-message-preview__dynamic`} data-kind={mode}>
      <SmartAvatar src={server ? identity.guildAvatarUrl : identity.botAvatarUrl} fallbackSrc={server ? undefined : "/assets/osaka-discord-avatar.png"}
        name={server ? identity.guildName || "Servidor" : "Bot"} type={server ? "server" : "user"} className="osk-dynamic-avatar" alt={label} />
    </span>;
  }
  const stars = mode === "avatar_stars";
  return <span className={`${className} osk-message-preview__dynamic`} data-kind={mode} role="img" aria-label={`${label} — exemplo`}>
    <svg viewBox={stars ? "0 0 320 144" : "0 0 96 96"} aria-hidden="true" focusable="false">
      {stars && <g fill="currentColor" opacity=".45"><path d="m42 30 3 7 7 3-7 3-3 7-3-7-7-3 7-3zm229 67 3 7 7 3-7 3-3 7-3-7-7-3 7-3zM248 24l2 4 4 2-4 2-2 4-2-4-4-2 4-2zM78 111l2 4 4 2-4 2-2 4-2-4-4-2 4-2z"/><circle cx="89" cy="37" r="1.5"/><circle cx="228" cy="108" r="1.5"/><circle cx="293" cy="65" r="1.5"/></g>}
      <g transform={stars ? "translate(112 24)" : undefined}>
        <circle cx="48" cy="48" r="44" fill="currentColor" opacity=".12"/>
        <circle cx="48" cy="36" r="13" fill="currentColor" opacity=".7"/>
        <path d="M22 77c0-16 11-25 26-25s26 9 26 25" fill="currentColor" opacity=".7"/>
      </g>
    </svg>
    {!icon && <small>Exemplo</small>}
  </span>;
}

interface MediaSlotProps {
  urlField?: DashboardFieldDefinition;
  modeField?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  className: string;
  alt: string;
  fallbackLabel: string;
}

export function ImageSlot({ urlField, modeField, draft, interactive, selectedFieldId, onSelectField, className, alt, fallbackLabel }: MediaSlotProps) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const dynamic = ["member", "inviter", "server", "bot", "avatar_stars"].includes(modeValue);
  const custom = !modeField || modeValue === "custom" || !modeValue;
  const validUrl = custom && isValidPreviewUrl(url);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); }, [url, modeValue]);
  const targetField = custom && urlField ? urlField : modeField ?? urlField;
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  if (!interactive && !dynamic && !validUrl) return null;
  return <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField}
    className={`${className}-wrap`} placeholder={label} ariaLabel={failed ? `Trocar imagem: ${alt}` : `Configurar ${alt.toLocaleLowerCase("pt-BR")}`}>
    {dynamic ? <DynamicMedia mode={modeValue} label={label} className={className} />
      : validUrl ? <MessageImage src={url} alt={alt} className={className} placeholder={label} onFailureChange={interactive ? setFailed : undefined} />
        : <span className="osk-message-preview__image-placeholder osk-media-empty">+ {fallbackLabel}</span>}
  </EditableRegion>;
}

export function IconSlot({ urlField, modeField, draft, interactive, selectedFieldId, onSelectField, alt, fallbackLabel }: Omit<MediaSlotProps, "className">) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const dynamic = ["member", "inviter", "server", "bot"].includes(modeValue);
  const custom = !modeField || modeValue === "custom" || !modeValue;
  const validUrl = custom && isValidPreviewUrl(url);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); }, [url, modeValue]);
  const targetField = custom && urlField ? urlField : modeField ?? urlField;
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  if (!dynamic && !validUrl && !interactive) return null;
  return <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField}
    className="osk-message-preview__icon-wrap" placeholder={label} ariaLabel={failed ? `Trocar imagem: ${alt}` : `Configurar ${alt.toLocaleLowerCase("pt-BR")}`}>
    {dynamic ? <DynamicMedia mode={modeValue} label={label} className="osk-message-preview__icon" icon />
      : validUrl ? <MessageImage src={url} alt={alt} className="osk-message-preview__icon" placeholder={label} onFailureChange={interactive ? setFailed : undefined} />
        : <span className="osk-message-preview__icon-placeholder" title={label}>+</span>}
  </EditableRegion>;
}
