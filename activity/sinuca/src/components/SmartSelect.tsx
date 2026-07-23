import { Check, ChevronDown, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

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
  const [position, setPosition] = useState({ left: 0, top: 0, width: 220 });
  const rootRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const selected = options.find((option) => option.value === value) ?? null;
  const searchable = options.length > 8;
  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("pt-BR");
    if (!normalized) return options;
    return options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(normalized));
  }, [options, query]);

  const updatePosition = useCallback(() => {
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.max(220, rect.width);
    const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));
    setPosition({ left, top: rect.bottom + 7, width });
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePosition();
    function handlePointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) setOpen(false);
    }
    function handleKeyDown(event: KeyboardEvent) { if (event.key === "Escape") setOpen(false); }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, updatePosition]);

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
  const layerStyle = {
    "--osk-select-left": `${position.left}px`,
    "--osk-select-top": `${position.top}px`,
    "--osk-select-width": `${position.width}px`,
  } as CSSProperties;
  const layer = open ? <div className="osk-select-layer" style={layerStyle}>
    <button type="button" className="osk-select-backdrop" onClick={() => setOpen(false)} aria-label="Fechar opções" />
    <div className="osk-select-popover" ref={popoverRef} role="presentation">
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
  </div> : null;

  return <div className="osk-select" data-open={open || undefined} data-disabled={disabled || undefined} ref={rootRef}>
    <button type="button" id={id} className="osk-select-trigger" aria-haspopup="listbox" aria-expanded={open} aria-controls={listboxId} disabled={disabled} onClick={() => { updatePosition(); setOpen((current) => !current); }}>
      <span className="osk-select-value">{selected ? selected.label : <span className="osk-select-placeholder">{placeholder ?? "Selecione"}</span>}</span>
      <ChevronDown size={16} className="osk-select-chev" aria-hidden="true" />
    </button>
    {layer && createPortal(layer, document.body)}
  </div>;
}
