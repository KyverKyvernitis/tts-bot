import { AlertCircle } from "lucide-react";
import { useEffect, useRef, type KeyboardEvent } from "react";
import type { ModuleArea } from "./moduleAreas";

interface Props { sectionId: string; areas: ModuleArea[]; selected: string; changed: Set<string>; invalid?: Set<string>; onSelect(id: string): void }
export function ModuleNavigation({ sectionId, areas, selected, changed, invalid = new Set(), onSelect }: Props) {
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navRef.current;
    const tab = document.getElementById(`module-tab-${sectionId}-${selected}`);
    if (!nav || !tab) return;
    const container = nav.getBoundingClientRect();
    const button = tab.getBoundingClientRect();
    const movement = button.left < container.left + 6 ? button.left - container.left - 6
      : button.right > container.right - 6 ? button.right - container.right + 6 : 0;
    if (movement) nav.scrollTo({ left: nav.scrollLeft + movement, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [sectionId, selected]);
  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = areas.length - 1;
    const next = event.key === "ArrowRight" ? (index + 1) % areas.length : event.key === "ArrowLeft" ? (index + last) % areas.length : event.key === "Home" ? 0 : event.key === "End" ? last : -1;
    if (next < 0) return;
    event.preventDefault(); onSelect(areas[next].id);
    document.getElementById(`module-tab-${sectionId}-${areas[next].id}`)?.focus({ preventScroll: true });
  }
  return <nav ref={navRef} className="osk-module-tabs" role="tablist" aria-label="Áreas do módulo">
    {areas.map((item, index) => <button key={item.id} type="button" role="tab" id={`module-tab-${sectionId}-${item.id}`} aria-label={item.label} aria-describedby={invalid.has(item.id) || changed.has(item.id) ? `module-tab-status-${sectionId}-${item.id}` : undefined} aria-controls={`module-area-${sectionId}-${item.id}`} aria-selected={selected === item.id} tabIndex={selected === item.id ? 0 : -1} onClick={() => onSelect(item.id)} onKeyDown={event => move(event, index)}>
      {item.label}{invalid.has(item.id) ? <AlertCircle className="osk-area-error" size={14} aria-hidden="true" /> : changed.has(item.id) ? <span className="osk-area-change" aria-hidden="true" /> : null}
      {(invalid.has(item.id) || changed.has(item.id)) && <span className="osk-sr-only" id={`module-tab-status-${sectionId}-${item.id}`}>{invalid.has(item.id) ? "Contém campos para revisar" : "Contém alterações não salvas"}</span>}
    </button>)}
  </nav>;
}
