import { ArrowDown, ArrowUp, GripVertical, Plus, Trash2 } from "lucide-react";
import { stringifyDashboardValue } from "../../app/dashboardFieldValues";
import type { DashboardFieldDefinition } from "../../types/dashboard";

export function StringListEditor({ field, value, onChange }: { field: DashboardFieldDefinition; value: unknown; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const items = Array.isArray(value) ? value.map(String) : stringifyDashboardValue(value).split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const update = (index: number, next: string) => onChange(field, items.map((item, itemIndex) => itemIndex === index ? next : item));
  const remove = (index: number) => onChange(field, items.filter((_, itemIndex) => itemIndex !== index));
  const move = (index: number, direction: -1 | 1) => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= items.length) return;
    const next = [...items];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onChange(field, next);
  };
  return <div className="osk-string-list-editor">
    {items.map((item, index) => <div key={index}>
      <GripVertical size={15} />
      <input aria-label={`${field.label}, item ${index + 1}`} value={item} onChange={(event) => update(index, event.target.value)} placeholder={`Item ${index + 1}`} />
      <button type="button" onClick={() => move(index, -1)} disabled={index === 0} aria-label="Mover para cima"><ArrowUp size={14} /></button>
      <button type="button" onClick={() => move(index, 1)} disabled={index === items.length - 1} aria-label="Mover para baixo"><ArrowDown size={14} /></button>
      <button type="button" data-danger onClick={() => remove(index)} aria-label="Remover item"><Trash2 size={14} /></button>
    </div>)}
    <button type="button" className="osk-add-row" onClick={() => onChange(field, [...items, ""])}><Plus size={15} />Adicionar item</button>
  </div>;
}
