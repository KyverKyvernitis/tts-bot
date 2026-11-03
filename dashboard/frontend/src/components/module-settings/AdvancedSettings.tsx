import { ChevronDown, SlidersHorizontal, type LucideIcon } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { dashboardFieldError } from "../../app/dashboardFormValidation";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { sectionEditorValuesEqual } from "../sectionEditorModel";
import { useFieldFeedback } from "./FieldFeedback";

interface AdvancedSettingsProps {
  title: string;
  summary: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  children: ReactNode;
  icon?: LucideIcon;
  className?: string;
}

export function advancedSettingsState(
  fields: DashboardFieldDefinition[],
  values: Record<string, unknown>,
  draft: Record<string, unknown>,
  errors: Record<string, string>,
) {
  const changed = fields.some(field => !sectionEditorValuesEqual(values[field.id], draft[field.id]));
  const invalid = fields.some(field => Boolean(errors[field.id]) || Boolean(dashboardFieldError(field, draft[field.id])));
  return { changed, invalid };
}

/** Native details keeps every field mounted, including existing hidden values. */
export function AdvancedSettings({
  title, summary, fields, values, draft, children, icon: Icon = SlidersHorizontal, className = "",
}: AdvancedSettingsProps) {
  const { errors } = useFieldFeedback();
  const state = advancedSettingsState(fields, values, draft, errors);
  const detailsRef = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    if ((state.changed || state.invalid) && detailsRef.current) detailsRef.current.open = true;
  }, [state.changed, state.invalid]);

  return <details
    ref={detailsRef}
    className={`osk-module-advanced osk-workspace-advanced ${className}`.trim()}
    data-changed={state.changed || undefined}
    data-invalid={state.invalid || undefined}
  >
    <summary>
      <Icon size={18} aria-hidden="true" />
      <span className="osk-advanced-heading"><strong>{title}</strong><small>{summary}</small></span>
      {state.invalid ? <span className="osk-advanced-state" data-error="true">Revisar</span> : state.changed ? <span className="osk-advanced-state">Alterado</span> : null}
      <ChevronDown className="osk-advanced-chevron" size={17} aria-hidden="true" />
    </summary>
    <div>{children}</div>
  </details>;
}
