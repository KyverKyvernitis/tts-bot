import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
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
    {!items.length && <div className="osk-workspace-empty-list">Nenhum item adicionado.</div>}
    {items.map((item, index) => <div key={index}>
      <span className="osk-string-list-number" aria-hidden="true">{index + 1}</span>
      <input aria-label={`${field.label}, item ${index + 1}`} value={item} onChange={(event) => update(index, event.target.value)} placeholder={`Item ${index + 1}`} />
      <button type="button" onClick={() => move(index, -1)} disabled={index === 0} aria-label={`Mover item ${index + 1} para cima`} title="Mover para cima"><ArrowUp size={16} /></button>
      <button type="button" onClick={() => move(index, 1)} disabled={index === items.length - 1} aria-label={`Mover item ${index + 1} para baixo`} title="Mover para baixo"><ArrowDown size={16} /></button>
      <button type="button" data-danger onClick={() => remove(index)} aria-label={`Remover item ${index + 1}`} title="Remover"><Trash2 size={16} /></button>
    </div>)}
    <button type="button" className="osk-add-row" onClick={() => onChange(field, [...items, ""])}><Plus size={15} />Adicionar item</button>
  </div>;
}
