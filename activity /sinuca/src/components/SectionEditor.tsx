import { ChevronLeft, Settings } from "lucide-react";
import type {
  DashboardFieldDefinition,
  DashboardSectionDefinition,
  DashboardSectionSummary,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { shortStatusLabel, statusClass } from "../moduleCatalog";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  summary: DashboardSectionSummary | undefined;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
  onBack(): void;
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(value) : "";
  return String(value);
}

function displayValue(field: DashboardFieldDefinition, value: unknown): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if ((field.type === "channel" || field.type === "role") && Number(value || 0) > 0) {
    return field.type === "channel" ? `#${value}` : `@${value}`;
  }
  if (field.type === "select") {
    const raw = stringifyValue(value);
    return field.options?.find((item) => item.value === raw)?.label ?? raw;
  }
  const text = stringifyValue(value).trim();
  return text || "Não configurado";
}

export function SectionEditor({
  section,
  module,
  summary,
  values,
  draft,
  onChange,
  onBack,
}: SectionEditorProps) {
  const state = statusClass(summary);
  const Icon = module?.icon ?? Settings;

  return (
    <section className="osk-page">
      <button className="osk-back-btn" onClick={onBack}>
        <ChevronLeft size={14} />
        Início
      </button>

      <div className="osk-section-head">
        <span className="osk-section-icon">
          <Icon size={22} />
        </span>
        <div>
          <h1>{section.label}</h1>
          <p>{module?.description ?? section.description}</p>
        </div>
        <span className="osk-badge" data-state={state}>
          {shortStatusLabel(summary)}
        </span>
      </div>

      <div className="osk-fields">
        {section.fields.map((field) => {
          const current = draft[field.id];
          const changed = draft[field.id] !== values[field.id];
          return (
            <div
              key={field.id}
              className="osk-field"
              data-type={field.type}
              data-changed={changed}
            >
              <div className="osk-field-head">
                <div>
                  <strong>{field.label}</strong>
                  {field.description && <small>{field.description}</small>}
                </div>
                {changed && (
                  <span className="osk-badge" data-state="changed">
                    alterado
                  </span>
                )}
              </div>

              {field.type === "boolean" ? (
                <label className="osk-switch">
                  <input
                    type="checkbox"
                    checked={Boolean(current)}
                    onChange={(event) => onChange(field, event.target.checked)}
                  />
                  <span className="osk-switch-track" />
                  <span className="osk-switch-label">
                    {Boolean(current) ? "Ligado" : "Desligado"}
                  </span>
                </label>
              ) : field.type === "select" ? (
                <select
                  value={stringifyValue(current)}
                  onChange={(event) => onChange(field, event.target.value)}
                >
                  {(field.options ?? []).map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : field.type === "textarea" ? (
                <textarea
                  value={stringifyValue(current)}
                  maxLength={field.maxLength}
                  placeholder={field.placeholder}
                  onChange={(event) => onChange(field, event.target.value)}
                />
              ) : (
                <input
                  type={field.type === "number" ? "number" : "text"}
                  min={field.min}
                  max={field.max}
                  maxLength={field.maxLength}
                  value={stringifyValue(current)}
                  placeholder={
                    field.placeholder ??
                    (field.type === "channel"
                      ? "ID ou menção do canal"
                      : field.type === "role"
                        ? "ID ou menção do cargo"
                        : field.type === "url"
                          ? "https://..."
                          : "")
                  }
                  onChange={(event) => onChange(field, event.target.value)}
                />
              )}

              <span className="osk-field-hint">
                Atual: <strong>{displayValue(field, values[field.id])}</strong>
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
