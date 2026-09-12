import { useState } from "react";
import type { MessageEditorView } from "./messageEditorModel";

export function useMessageEditorViewState() {
  const [view, setView] = useState<MessageEditorView>("canvas");
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);
  const [selectedColorSlot, setSelectedColorSlot] = useState<number | null>(null);
  const [contextAnchorFieldId, setContextAnchorFieldId] = useState<string | null>(null);

  function resetViewState() {
    setSelectedFieldId(null);
    setEditingFieldId(null);
    setSelectedColorSlot(null);
    setContextAnchorFieldId(null);
    setView("canvas");
  }

  return {
    view,
    setView,
    selectedFieldId,
    setSelectedFieldId,
    editingFieldId,
    setEditingFieldId,
    selectedColorSlot,
    setSelectedColorSlot,
    contextAnchorFieldId,
    setContextAnchorFieldId,
    resetViewState,
  };
}
