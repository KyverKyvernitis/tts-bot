import { Diamond, Spade, type LucideIcon } from "lucide-react";
import type { CSSProperties } from "react";
import { findModuleMeta } from "../moduleCatalog";

interface ModuleArtworkProps { moduleId: string; fallbackIcon: LucideIcon }

export function ModuleArtwork({ moduleId, fallbackIcon: FallbackIcon }: ModuleArtworkProps) {
  const kind = findModuleMeta(moduleId)?.id;
  return <span className={`osk-module-artwork osk-art-${kind || "other"}`} aria-hidden="true">
    {kind === "welcome" && <span className="osk-mini-message"><i className="osk-mini-avatar" /><span className="osk-mini-bubble"><i /><i /><i /></span><i className="osk-mini-message-reply" /></span>}
    {kind === "forms" && <span className="osk-mini-form">{[0, 1, 2].map((row) => <span key={row}><i /><b /></span>)}</span>}
    {kind === "tickets" && <span className="osk-mini-ticket"><span className="osk-mini-stub"><i /><i /><i /></span><span className="osk-mini-ticket-lines"><i /><i /></span></span>}
    {kind === "color_roles" && <span className="osk-mini-palette">{[0, 1, 2, 3, 4].map((tone) => <i key={tone} style={{ "--tone": tone } as CSSProperties} />)}</span>}
    {kind === "birthday" && <span className="osk-mini-calendar"><b /><span>{Array.from({ length: 12 }, (_, index) => <i key={index} data-marked={index === 6 || undefined} />)}</span></span>}
    {kind === "tts" && <span className="osk-mini-wave">{[12, 22, 37, 51, 31, 58, 43, 24, 12].map((height, index) => <i key={index} style={{ height }} />)}</span>}
    {kind === "economy" && <span className="osk-casino-scene">
      <span className="osk-playing-card osk-playing-card-back"><b>A</b><Diamond size={14} /></span>
      <span className="osk-playing-card osk-playing-card-front"><b>A</b><Spade size={14} /></span>
      <span className="osk-casino-chip osk-casino-chip-stack"><span><Spade size={13} /></span></span>
      <span className="osk-casino-chip osk-casino-chip-loose"><span /></span>
    </span>}
    {!kind && <FallbackIcon size={30} />}
  </span>;
}
