import { Check, Plus, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import type { SmartSelectOption } from "../SmartSelect";

export function RoleMultiEditor({ field, value, options, onChange }: { field: DashboardFieldDefinition; value: unknown; options: SmartSelectOption[]; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const selected = Array.isArray(value) ? value.map(String) : [];
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [working, setWorking] = useState<string[]>(selected);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (!open) setWorking(selected); }, [open, value]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : triggerRef.current;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => searchRef.current?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      )).filter((element) => !element.hasAttribute("hidden"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKey);
      window.requestAnimationFrame(() => previousFocus?.focus({ preventScroll: true }));
    };
  }, [open]);

  if (!options.length) return <input aria-label={field.label} value={selected.join(", ")} placeholder="IDs separados por vírgula" onChange={(event) => onChange(field, event.target.value.split(/[\s,]+/).filter(Boolean))} />;

  const selectedOptions = selected.map((id) => options.find((option) => option.value === id) ?? { value: id, label: `Cargo ${id}` });
  const needle = query.trim().toLocaleLowerCase("pt-BR");
  const filtered = needle ? options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(needle)) : options;
  const toggle = (roleId: string) => setWorking((current) => current.includes(roleId) ? current.filter((id) => id !== roleId) : [...current, roleId]);

  const modal = open ? <div className="osk-root osk-multi-sheet" role="dialog" aria-modal="true" aria-label={`Selecionar ${field.label}`}>
    <button type="button" className="osk-multi-sheet__backdrop" onClick={() => setOpen(false)} aria-label="Fechar" />
    <div className="osk-multi-sheet__panel" ref={panelRef}>
      <header><div><strong>{field.label}</strong><small>{working.length} selecionado{working.length === 1 ? "" : "s"}</small></div><button type="button" onClick={() => setOpen(false)} aria-label="Fechar"><X size={18} /></button></header>
      <label className="osk-multi-sheet__search"><Search size={16} /><input ref={searchRef} aria-label="Buscar cargo" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar cargo" /></label>
      <div className="osk-multi-sheet__list">
        {filtered.map((option) => <button key={option.value} type="button" data-selected={working.includes(option.value) || undefined} onClick={() => toggle(option.value)}>
          <span><strong>{option.label}</strong>{option.hint && <small>{option.hint}</small>}</span>{working.includes(option.value) && <Check size={17} />}
        </button>)}
        {!filtered.length && <div className="osk-message-empty">Nenhum cargo encontrado.</div>}
      </div>
      <footer><button type="button" className="osk-secondary-button" onClick={() => { setWorking([]); }}>Limpar</button><button type="button" className="osk-primary-button" onClick={() => { onChange(field, working); setOpen(false); }}>Concluir</button></footer>
    </div>
  </div> : null;

  return <div className="osk-role-multi">
    <div className="osk-role-multi__chips">
      {selectedOptions.map((option) => <span key={option.value}>{option.label}<button type="button" onClick={() => onChange(field, selected.filter((id) => id !== option.value))} aria-label={`Remover ${option.label}`}><X size={13} /></button></span>)}
      {!selectedOptions.length && <small>Nenhum cargo selecionado.</small>}
    </div>
    <button ref={triggerRef} type="button" className="osk-secondary-button osk-role-multi__open" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setWorking(selected); setOpen(true); }}><Plus size={15} />Selecionar cargos</button>
    {modal && createPortal(modal, document.body)}
  </div>;
}
