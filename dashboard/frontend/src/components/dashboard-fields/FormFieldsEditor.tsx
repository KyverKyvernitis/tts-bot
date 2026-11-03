import { ArrowDown, ArrowUp, ChevronDown, Copy, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { normalizeDashboardFormField } from "../../app/dashboardFieldValues";
import type { DashboardFieldDefinition, DashboardFormField } from "../../types/dashboard";

export function FormFieldsEditor({ field, value, onChange }: { field: DashboardFieldDefinition; value: unknown; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const fields = Array.isArray(value) ? value.map((item, index) => normalizeDashboardFormField(item, index)) : [];
  const [openId, setOpenId] = useState<string | null>(fields[0]?.id ?? null);
  const rootRef = useRef<HTMLDivElement>(null);
  const newQuestionRef = useRef<string | null>(null);
  useEffect(() => {
    if (!newQuestionRef.current || newQuestionRef.current !== openId) return;
    const input = rootRef.current?.querySelector<HTMLInputElement>(".osk-form-question-panel input");
    if (input) {
      input.focus({ preventScroll: true });
      input.scrollIntoView({ block: "center", behavior: "auto" });
      newQuestionRef.current = null;
    }
  }, [openId, value]);
  const update = (index: number, patch: Partial<DashboardFormField>) => onChange(field, fields.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  const remove = (index: number) => {
    const target = fields[index];
    if ((target.label || target.placeholder) && !window.confirm(`Excluir “${target.label || `Pergunta ${index + 1}`}”?`)) return;
    onChange(field, fields.filter((_, itemIndex) => itemIndex !== index));
    if (openId === target.id) {
      const nextId = fields[index + 1]?.id || fields[index - 1]?.id || null;
      newQuestionRef.current = nextId;
      setOpenId(nextId);
    }
  };
  const add = () => {
    if (fields.length >= 5) return;
    const next = normalizeDashboardFormField({ id: `field-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}` }, fields.length);
    onChange(field, [...fields, next]);
    newQuestionRef.current = next.id;
    setOpenId(next.id);
  };
  const duplicate = (index: number) => {
    if (fields.length >= 5) return;
    const copy = { ...fields[index], id: `${fields[index].id}-copy-${Date.now().toString(36)}`, label: `${fields[index].label} (cópia)` };
    onChange(field, [...fields.slice(0, index + 1), copy, ...fields.slice(index + 1)]);
    newQuestionRef.current = copy.id;
    setOpenId(copy.id);
  };
  const move = (index: number, direction: -1 | 1) => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= fields.length) return;
    const next = [...fields];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onChange(field, next);
  };

  return <div className="osk-form-fields-editor" ref={rootRef}>
    <div className="osk-form-fields-limit"><strong>{fields.length} de 5 perguntas</strong><span>{fields.filter(item => item.enabled).length} visíveis</span></div>
    {!fields.length && <div className="osk-workspace-empty-list">Nenhuma pergunta adicionada.</div>}
    {fields.map((item, index) => {
      const open = openId === item.id;
      const panelId = `form-question-panel-${item.id || index}`;
      return <article key={item.id || index} data-open={open || undefined} data-enabled={item.enabled || undefined}>
        <div className="osk-form-question-head">
          <button type="button" className="osk-form-question-summary" onClick={() => setOpenId((current) => current === item.id ? null : item.id)} aria-expanded={open} aria-controls={panelId} aria-label={`Editar pergunta ${index + 1}`}>
            <span className="osk-question-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span><span><strong>{item.label || `Pergunta ${index + 1}`}</strong><small>{item.enabled ? `${item.required ? "Obrigatória" : "Opcional"} · ${item.long ? "Resposta longa" : "Resposta curta"}` : "Oculta no formulário"}</small></span><ChevronDown size={16} />
          </button>
          {open && <div className="osk-form-question-actions">
            <button type="button" onClick={() => duplicate(index)} disabled={fields.length >= 5} aria-label={`Duplicar pergunta ${index + 1}`} title="Duplicar"><Copy size={16} /></button>
            <button type="button" onClick={() => move(index, -1)} disabled={index === 0} aria-label={`Mover pergunta ${index + 1} para cima`} title="Mover para cima"><ArrowUp size={16} /></button>
            <button type="button" onClick={() => move(index, 1)} disabled={index === fields.length - 1} aria-label={`Mover pergunta ${index + 1} para baixo`} title="Mover para baixo"><ArrowDown size={16} /></button>
            <button type="button" data-danger onClick={() => remove(index)} aria-label={`Remover pergunta ${index + 1}`} title="Remover"><Trash2 size={16} /></button>
          </div>}
        </div>
        {open && <div className="osk-form-question-panel" id={panelId}>
          <div className="osk-form-question-panel-inner">
            <div className="osk-inline-grid"><label><span>Pergunta</span><input value={item.label} maxLength={45} placeholder="Ex.: Qual é o motivo do contato?" onChange={(event) => update(index, { label: event.target.value })} /></label><label><span>Título para a equipe</span><input value={item.response_label} maxLength={80} placeholder="Ex.: Motivo do contato" onChange={(event) => update(index, { response_label: event.target.value })} /></label></div>
            <label><span>Exemplo ou instrução</span><input value={item.placeholder} maxLength={100} onChange={(event) => update(index, { placeholder: event.target.value })} /></label>
            <div className="osk-form-question-switches">
              <SwitchRow label="Exibir pergunta" description="Exibe este campo no formulário." checked={item.enabled} onChange={(checked) => update(index, { enabled: checked })} />
              <SwitchRow label="Resposta obrigatória" description="Impede o envio sem preencher." checked={item.required} onChange={(checked) => update(index, { required: checked })} />
              <SwitchRow label="Campo de resposta longa" description="Usa uma área maior para textos extensos." checked={item.long} onChange={(checked) => update(index, { long: checked, max_length: checked ? Math.max(item.max_length, 1000) : Math.min(item.max_length, 120) })} />
              <SwitchRow label="Mostrar no resumo" description="Inclui a resposta na mensagem enviada à equipe." checked={item.show_in_response} onChange={(checked) => update(index, { show_in_response: checked })} />
            </div>
          </div>
        </div>}
      </article>;
    })}
    <button type="button" className="osk-add-row" onClick={add} disabled={fields.length >= 5}><Plus size={17} />{fields.length >= 5 ? "Limite de 5 perguntas" : "Adicionar pergunta"}</button>
  </div>;
}

function SwitchRow({ label, description, checked, onChange }: { label: string; description: string; checked: boolean; onChange(value: boolean): void }) {
  return <label className="osk-inline-switch-row"><span><strong>{label}</strong><small>{description}</small></span><span className="osk-switch"><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /><span className="osk-switch-track" /></span></label>;
}
