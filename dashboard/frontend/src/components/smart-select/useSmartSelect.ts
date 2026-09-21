import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type TransitionEvent,
} from "react";
import {
  adjacentEnabledSmartSelectIndex,
  filterSmartSelectOptions,
  firstEnabledSmartSelectIndex,
  smartSelectEstimatedHeight,
  smartSelectPosition,
  type SmartSelectOption,
} from "./smartSelectModel";
import { anchoredSelectPosition } from "./anchoredSelectPosition";

const SELECT_TRANSITION_MS = 220;

export function useSmartSelect(options: SmartSelectOption[], value: string, disabled: boolean | undefined, onChange: (value: string) => void, presentation: "adaptive" | "anchored" = "adaptive") {
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 220, maxHeight: 420, placement: "below" as "above" | "below" });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const focusedOnOpenRef = useRef(false);
  const closeTimerRef = useRef<number | null>(null);
  const selected = options.find((option) => option.value === value) ?? null;
  const searchable = options.length > 8;
  const filteredOptions = useMemo(() => filterSmartSelectOptions(options, query), [options, query]);

  const updatePosition = useCallback(() => {
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;
    const measuredHeight = popoverRef.current?.scrollHeight || popoverRef.current?.getBoundingClientRect().height || smartSelectEstimatedHeight(options.length);
    if (presentation === "anchored") {
      const viewport = window.visualViewport;
      setPosition(anchoredSelectPosition(rect, {
        left: viewport?.offsetLeft || 0,
        top: viewport?.offsetTop || 0,
        width: viewport?.width || window.innerWidth,
        height: viewport?.height || window.innerHeight,
      }, measuredHeight));
    } else {
      setPosition({...smartSelectPosition(rect, window.innerWidth, window.innerHeight, measuredHeight), maxHeight: 420});
    }
  }, [options.length, presentation]);

  const finishClose = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    setMounted(false);
    setQuery("");
  }, []);

  const close = useCallback((restoreFocus = true) => {
    setVisible(false);
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = window.setTimeout(finishClose, SELECT_TRANSITION_MS + 70);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
  }, [finishClose]);

  const open = useCallback(() => {
    if (disabled) return;
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    updatePosition();
    setMounted(true);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => setVisible(true)));
  }, [disabled, updatePosition]);

  useEffect(() => {
    if (!mounted) return;
    function handlePointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) close(false);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown, { passive: true });
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    const viewport = window.visualViewport;
    viewport?.addEventListener("resize", updatePosition);
    viewport?.addEventListener("scroll", updatePosition);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      viewport?.removeEventListener("resize", updatePosition);
      viewport?.removeEventListener("scroll", updatePosition);
    };
  }, [close, mounted, updatePosition]);

  useEffect(() => {
    if (!mounted || presentation === "anchored" || !window.matchMedia("(max-width: 720px)").matches) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previousOverflow; };
  }, [mounted, presentation]);

  useEffect(() => {
    if (!visible) { focusedOnOpenRef.current = false; return; }
    const selectedIndex = filteredOptions.findIndex((option) => option.value === value);
    setActiveIndex(selectedIndex >= 0 && !filteredOptions[selectedIndex]?.disabled
      ? selectedIndex
      : firstEnabledSmartSelectIndex(filteredOptions));
    const focusTimer = window.setTimeout(() => {
      updatePosition();
      if (!focusedOnOpenRef.current) {
        focusedOnOpenRef.current = true;
        if (document.activeElement !== searchRef.current) listRef.current?.focus({ preventScroll: true });
        listRef.current?.querySelector<HTMLElement>('[data-selected="true"]')?.scrollIntoView({ block: "nearest" });
      }
    }, 40);
    return () => window.clearTimeout(focusTimer);
  }, [filteredOptions, updatePosition, value, visible]);

  useEffect(() => () => {
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, []);

  const commit = useCallback((optionValue: string) => {
    if (options.find((option) => option.value === optionValue)?.disabled) return;
    onChange(optionValue);
    close();
  }, [close, onChange, options]);

  const handleOptionsKeyDown = useCallback((event: KeyboardEvent<HTMLInputElement | HTMLUListElement>) => {
    if (event.key === "Tab") {
      const toSearch = event.currentTarget === listRef.current && event.shiftKey && searchable;
      const toList = event.currentTarget === searchRef.current && !event.shiftKey;
      if (toSearch || toList) { event.preventDefault(); (toSearch ? searchRef.current : listRef.current)?.focus({ preventScroll: true }); }
      else close(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => adjacentEnabledSmartSelectIndex(filteredOptions, index < 0 ? -1 : index, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => adjacentEnabledSmartSelectIndex(filteredOptions, index < 0 ? filteredOptions.length : index, -1));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(firstEnabledSmartSelectIndex(filteredOptions));
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(adjacentEnabledSmartSelectIndex(filteredOptions, filteredOptions.length, -1));
    } else if (event.key === "Enter" || (event.key === " " && event.currentTarget.tagName !== "INPUT")) {
      event.preventDefault();
      const option = filteredOptions[activeIndex];
      if (option && !option.disabled) commit(option.value);
    }
  }, [activeIndex, close, commit, filteredOptions, searchable]);

  const handleTransitionEnd = useCallback((event: TransitionEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || visible) return;
    if (event.propertyName === "opacity" || event.propertyName === "transform") finishClose();
  }, [finishClose, visible]);

  return {
    mounted,
    visible,
    query,
    setQuery,
    activeIndex,
    setActiveIndex,
    position,
    rootRef,
    triggerRef,
    popoverRef,
    listRef,
    searchRef,
    selected,
    searchable,
    filteredOptions,
    open,
    close,
    commit,
    handleOptionsKeyDown,
    handleTransitionEnd,
  };
}
