import type { KeyboardEvent } from "react";
import type { ModuleArea } from "./moduleAreas";

interface Props { sectionId: string; areas: ModuleArea[]; selected: string; changed: Set<string>; onSelect(id: string): void }
export function ModuleNavigation({ sectionId, areas, selected, changed, onSelect }: Props) {
  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = areas.length - 1;
    const next = event.key === "ArrowRight" ? (index + 1) % areas.length : event.key === "ArrowLeft" ? (index + last) % areas.length : event.key === "Home" ? 0 : event.key === "End" ? last : -1;
    if (next < 0) return;
    event.preventDefault(); onSelect(areas[next].id);
    document.getElementById(`module-tab-${sectionId}-${areas[next].id}`)?.focus({ preventScroll: true });
  }
  return <nav className="osk-module-tabs" role="tablist" aria-label="Áreas do módulo">
    {areas.map((item, index) => <button key={item.id} type="button" role="tab" id={`module-tab-${sectionId}-${item.id}`} aria-controls={`module-area-${sectionId}-${item.id}`} aria-selected={selected === item.id} tabIndex={selected === item.id ? 0 : -1} onClick={() => onSelect(item.id)} onKeyDown={event => move(event, index)}>
      {item.label}{changed.has(item.id) && <span className="osk-area-change" aria-label="Contém alterações não salvas" />}
    </button>)}
  </nav>;
}
