import { Check, ChevronDown, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export interface SmartSelectOption {
  value: string;
  label: string;
  hint?: string;
}

interface SmartSelectProps {
  value: string;
  options: SmartSelectOption[];
  onChange(value: string): void;
  placeholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
  id?: string;
}

export function SmartSelect({ value, options, onChange, placeholder, emptyLabel, disabled, id }: SmartSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const selected = options.find((option) => option.value === value) ?? null;
  const searchable = options.length > 8;
  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("pt-BR");
    if (!normalized) return options;
    return options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(normalized));
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent | TouchEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    function handleKeyDown(event: KeyboardEvent) { if (event.key === "Escape") setOpen(false); }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!open) { setQuery(""); return; }
    const idx = filteredOptions.findIndex((option) => option.value === value);
    setActiveIndex(idx >= 0 ? idx : 0);
    window.setTimeout(() => searchable ? searchRef.current?.focus() : listRef.current?.focus(), 30);
  }, [filteredOptions, open, searchable, value]);

  function commit(optionValue: string) {
    onChange(optionValue);
    setOpen(false);
  }

  function handleListKeyDown(event: React.KeyboardEvent<HTMLUListElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((idx) => Math.min(filteredOptions.length - 1, idx + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((idx) => Math.max(0, idx - 1));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const option = filteredOptions[activeIndex];
      if (option) commit(option.value);
    }
  }

  const listboxId = id ? `${id}-listbox` : undefined;

  return <div className="osk-select" data-open={open || undefined} data-disabled={disabled || undefined} ref={rootRef}>
    <button type="button" id={id} className="osk-select-trigger" aria-haspopup="listbox" aria-expanded={open} aria-controls={listboxId} disabled={disabled} onClick={() => setOpen((current) => !current)}>
      <span className="osk-select-value">{selected ? selected.label : <span className="osk-select-placeholder">{placeholder ?? "Selecione"}</span>}</span>
      <ChevronDown size={16} className="osk-select-chev" aria-hidden="true" />
    </button>

    {open && <>
      <button type="button" className="osk-select-backdrop" onClick={() => setOpen(false)} aria-label="Fechar opções" />
      <div className="osk-select-popover">
        <header className="osk-select-mobile-header"><strong>Escolha uma opção</strong><button type="button" onClick={() => setOpen(false)} aria-label="Fechar"><X size={18} /></button></header>
        {searchable && <label className="osk-select-search"><Search size={15} /><input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar..." /></label>}
        <ul className="osk-select-menu" role="listbox" id={listboxId} ref={listRef} tabIndex={-1} aria-activedescendant={activeIndex >= 0 && filteredOptions[activeIndex] ? `${id}-opt-${activeIndex}` : undefined} onKeyDown={handleListKeyDown}>
          {filteredOptions.length === 0 && <li className="osk-select-empty" role="presentation">{emptyLabel ?? "Nenhuma opção disponível"}</li>}
          {filteredOptions.map((option, index) => <li key={option.value || "__empty"} id={id ? `${id}-opt-${index}` : undefined} role="option" aria-selected={option.value === value} className="osk-select-option" data-active={index === activeIndex || undefined} data-selected={option.value === value || undefined} onMouseEnter={() => setActiveIndex(index)} onClick={() => commit(option.value)}>
            <span className="osk-select-option-text"><span>{option.label}</span>{option.hint && <small>{option.hint}</small>}</span>
            {option.value === value && <Check size={14} aria-hidden="true" />}
          </li>)}
        </ul>
      </div>
    </>}
  </div>;
}
