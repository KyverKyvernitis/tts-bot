import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

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

/**
 * Dropdown/combobox escuro reutilizável.
 * Substitui o <select> nativo, que no Android/iOS abre um menu branco do
 * sistema e quebra o tema. Tudo aqui é HTML/CSS simples, sem dependência nova.
 */
export function SmartSelect({
  value,
  options,
  onChange,
  placeholder,
  emptyLabel,
  disabled,
  id,
}: SmartSelectProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const selected = options.find((option) => option.value === value) ?? null;

  useEffect(() => {
    if (!open) return;

    function handlePointerDown(event: MouseEvent | TouchEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }

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
    if (!open) return;
    const idx = options.findIndex((option) => option.value === value);
    setActiveIndex(idx >= 0 ? idx : 0);
    listRef.current?.focus();
  }, [open, options, value]);

  function commit(optionValue: string) {
    onChange(optionValue);
    setOpen(false);
  }

  function handleTriggerKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setOpen(true);
    }
  }

  function handleListKeyDown(event: React.KeyboardEvent<HTMLUListElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((idx) => Math.min(options.length - 1, idx + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((idx) => Math.max(0, idx - 1));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const option = options[activeIndex];
      if (option) commit(option.value);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  }

  const listboxId = id ? `${id}-listbox` : undefined;

  return (
    <div className="osk-select" data-open={open || undefined} data-disabled={disabled || undefined} ref={rootRef}>
      <button
        type="button"
        id={id}
        className="osk-select-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        disabled={disabled}
        onClick={() => setOpen((prev) => !prev)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="osk-select-value">
          {selected ? (
            selected.label
          ) : (
            <span className="osk-select-placeholder">{placeholder ?? "Selecione"}</span>
          )}
        </span>
        <ChevronDown size={15} className="osk-select-chev" aria-hidden="true" />
      </button>

      {open && (
        <ul
          className="osk-select-menu"
          role="listbox"
          id={listboxId}
          ref={listRef}
          tabIndex={-1}
          aria-activedescendant={activeIndex >= 0 && options[activeIndex] ? `${id}-opt-${activeIndex}` : undefined}
          onKeyDown={handleListKeyDown}
        >
          {options.length === 0 && (
            <li className="osk-select-empty" role="presentation">
              {emptyLabel ?? "Nenhuma opção disponível"}
            </li>
          )}
          {options.map((option, index) => (
            <li
              key={option.value}
              id={id ? `${id}-opt-${index}` : undefined}
              role="option"
              aria-selected={option.value === value}
              className="osk-select-option"
              data-active={index === activeIndex || undefined}
              data-selected={option.value === value || undefined}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => commit(option.value)}
            >
              <span className="osk-select-option-text">
                <span>{option.label}</span>
                {option.hint && <small>{option.hint}</small>}
              </span>
              {option.value === value && <Check size={14} aria-hidden="true" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
