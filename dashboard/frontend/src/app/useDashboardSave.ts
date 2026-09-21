import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { DashboardFieldDefinition, DashboardSectionSummary } from "../types/dashboard";
import { clearDashboardCommandsCache, patchDashboardSettings } from "../transport/dashboardApi";
import { DashboardHttpError } from "../transport/httpClient";
import type { DashboardNotice } from "./useDashboardSessionBootstrap";
import { dashboardChangedFieldErrors, reconcileSavedDraft } from "./dashboardFormValidation";
import { saveSuccessText } from "./appModel";
import { errorText } from "./errors";

interface Props {
  guildId: string | null; sectionId: string | null; fields: DashboardFieldDefinition[];
  values: Record<string, unknown>; draft: Record<string, unknown>; activeGuildRef: MutableRefObject<string | null>;
  setValues: Dispatch<SetStateAction<Record<string, unknown>>>; setDraft: Dispatch<SetStateAction<Record<string, unknown>>>;
  setSummary: Dispatch<SetStateAction<DashboardSectionSummary[]>>; setNotice: Dispatch<SetStateAction<DashboardNotice | null>>;
}
export function useDashboardSave({ guildId, sectionId, fields, values, draft, activeGuildRef, setValues, setDraft, setSummary, setNotice }: Props) {
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const pending = useRef(false);
  const sequence = useRef(0);
  useEffect(() => { sequence.current += 1; pending.current = false; setSaving(false); }, [guildId]);
  useEffect(() => { setFieldErrors({}); }, [guildId, sectionId]);
  const clearFieldError = useCallback((id?: string) => setFieldErrors(current => {
    if (!id) return {};
    if (!(id in current)) return current;
    const next = { ...current }; delete next[id]; return next;
  }), []);

  const handleSave = useCallback(async () => {
    if (!guildId || !sectionId || !fields.length || pending.current) return;
    const invalid = dashboardChangedFieldErrors(fields, draft);
    setFieldErrors(invalid);
    if (Object.keys(invalid).length) { setNotice({ type: "error", text: "Revise os campos indicados antes de salvar." }); return; }
    const request = ++sequence.current;
    const valid = () => sequence.current === request && activeGuildRef.current === guildId;
    const submitted = { ...draft };
    pending.current = true; setSaving(true); setNotice(null);
    try {
      const result = await patchDashboardSettings(guildId, Object.fromEntries(fields.map(field => [field.id, submitted[field.id]])));
      if (!valid()) return;
      const merged = { ...values, ...result.values };
      setValues(merged);
      setDraft(current => reconcileSavedDraft(submitted, current, merged));
      if (result.summary) setSummary(result.summary);
      if (result.saved.some(id => id === "general.bot_prefix" || id === "economy.input_mode")) clearDashboardCommandsCache(guildId);
      setNotice({ type: "success", text: saveSuccessText(result.saved.length, Boolean(result.summary_error)) });
    } catch (error) {
      if (!valid()) return;
      if (error instanceof DashboardHttpError && error.payload && typeof error.payload === "object") {
        const payload = error.payload as { field?: unknown };
        if (typeof payload.field === "string") setFieldErrors({ [payload.field]: error.message });
      }
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      if (sequence.current === request) { pending.current = false; setSaving(false); }
    }
  }, [guildId, sectionId, fields, values, draft, activeGuildRef, setValues, setDraft, setSummary, setNotice]);
  return { saving, fieldErrors, clearFieldError, handleSave };
}
