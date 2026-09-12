import { useCallback, useEffect, useRef, useState } from "react";
import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardSectionDefinition,
  DashboardTemplateVariables,
} from "../types/dashboard";
import {
  resolveSectionMessageEditor,
  type ActiveSectionMessageEditor,
} from "./sectionEditorModel";

interface UseSectionMessageEditorOptions {
  section: DashboardSectionDefinition;
  draft: Record<string, unknown>;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onActiveChange?(active: boolean): void;
}

export function useSectionMessageEditor({ section, draft, onChange, onActiveChange }: UseSectionMessageEditorOptions) {
  const [activeEditor, setActiveEditor] = useState<ActiveSectionMessageEditor | null>(null);
  const pageScrollRef = useRef(0);

  useEffect(() => {
    setActiveEditor(null);
  }, [section.id]);

  useEffect(() => {
    onActiveChange?.(Boolean(activeEditor));
  }, [activeEditor, onActiveChange]);

  const restorePagePosition = useCallback(() => {
    window.requestAnimationFrame(() => window.scrollTo({ top: pageScrollRef.current, behavior: "auto" }));
  }, []);

  const finishEditor = useCallback(() => {
    setActiveEditor(null);
    restorePagePosition();
  }, [restorePagePosition]);

  const openMessageEditor = useCallback((editor: DashboardMessageEditorDefinition, fallbackVariables?: DashboardTemplateVariables) => {
    const resolved = resolveSectionMessageEditor(section, editor, draft, fallbackVariables);
    if (!resolved) return;
    pageScrollRef.current = window.scrollY;
    setActiveEditor(resolved);
  }, [draft, section]);

  const closeEditorDiscard = useCallback(() => {
    if (!activeEditor) return;
    for (const field of activeEditor.fields) onChange(field, activeEditor.baseline[field.id]);
    finishEditor();
  }, [activeEditor, finishEditor, onChange]);

  return { activeEditor, openMessageEditor, finishEditor, closeEditorDiscard };
}
