import { Check, ChevronDown, Search, X } from "lucide-react";
import { type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { useSmartSelect } from "./smart-select/useSmartSelect";
import type { SmartSelectOption } from "./smart-select/smartSelectModel";

export type { SmartSelectOption } from "./smart-select/smartSelectModel";

interface SmartSelectProps {
  value: string;
  options: SmartSelectOption[];
  onChange(value: string): void;
  placeholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
}

export function SmartSelect({ value, options, onChange, placeholder, emptyLabel, disabled, id, ariaLabel }: SmartSelectProps) {
  const select = useSmartSelect(options, value, disabled, onChange);
  const listboxId = id ? `${id}-listbox` : undefined;
  const activeDescendant = id && select.activeIndex >= 0 && select.filteredOptions[select.activeIndex]
    ? `${id}-opt-${select.activeIndex}`
    : undefined;
  const layerStyle = {
    "--osk-select-left": `${select.position.left}px`,
    "--osk-select-top": `${select.position.top}px`,
    "--osk-select-width": `${select.position.width}px`,
  } as CSSProperties;

  const layer = select.mounted ? <div className="osk-select-layer" data-visible={select.visible || undefined} data-placement={select.position.placement} style={layerStyle} onTransitionEnd={select.handleTransitionEnd}>
    <button type="button" className="osk-select-backdrop" onClick={() => select.close()} aria-label="Fechar opções" tabIndex={select.visible ? 0 : -1} />
    <div className="osk-select-popover" ref={select.popoverRef} role="presentation" data-placement={select.position.placement}>
      <span className="osk-select-sheet-handle" aria-hidden="true" />
      <header className="osk-select-mobile-header">
        <strong>Escolha uma opção</strong>
        <button type="button" className="osk-select-close" onClick={() => select.close()} aria-label="Fechar"><X size={18} /></button>
      </header>
      {select.searchable && <label className="osk-select-search"><Search size={15} /><input ref={select.searchRef} role="combobox" aria-label={`Buscar em ${ariaLabel || "opções"}`} aria-autocomplete="list" aria-controls={listboxId} aria-expanded={select.visible} aria-activedescendant={activeDescendant} value={select.query} onChange={(event) => select.setQuery(event.target.value)} onKeyDown={select.handleOptionsKeyDown} placeholder="Buscar..." /></label>}
      <ul className="osk-select-menu" role="listbox" aria-label={ariaLabel || "Opções"} id={listboxId} ref={select.listRef} tabIndex={select.searchable ? -1 : 0} aria-activedescendant={activeDescendant} onKeyDown={select.handleOptionsKeyDown}>
        {select.filteredOptions.length === 0 && <li className="osk-select-empty" role="presentation">{emptyLabel ?? "Nenhuma opção disponível"}</li>}
        {select.filteredOptions.map((option, index) => <li key={option.value || "__empty"} id={id ? `${id}-opt-${index}` : undefined} role="option" aria-selected={option.value === value} aria-disabled={option.disabled || undefined} className="osk-select-option" data-active={!option.disabled && index === select.activeIndex || undefined} data-selected={option.value === value || undefined} data-disabled={option.disabled || undefined} onMouseEnter={() => { if (!option.disabled) select.setActiveIndex(index); }} onClick={() => { if (!option.disabled) select.commit(option.value); }}>
          <span className="osk-select-option-text"><span>{option.label}</span>{option.hint && <small>{option.hint}</small>}</span>
          {option.value === value && <Check size={14} aria-hidden="true" />}
        </li>)}
      </ul>
    </div>
  </div> : null;

  return <div className="osk-select" data-open={select.visible || undefined} data-disabled={disabled || undefined} ref={select.rootRef}>
    <button ref={select.triggerRef} type="button" id={id} className="osk-select-trigger" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={select.visible} aria-controls={listboxId} disabled={disabled} onClick={() => select.visible ? select.close() : select.open()}>
      <span className="osk-select-value">{select.selected ? select.selected.label : <span className="osk-select-placeholder">{placeholder ?? "Selecione"}</span>}</span>
      <ChevronDown size={16} className="osk-select-chev" aria-hidden="true" />
    </button>
    {layer && createPortal(layer, document.body)}
  </div>;
}
