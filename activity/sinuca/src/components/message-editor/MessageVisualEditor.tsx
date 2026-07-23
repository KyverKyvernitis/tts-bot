import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";

interface MessageVisualEditorProps {
  fields: DashboardFieldDefinition[];
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onFocusField?(field: DashboardFieldDefinition): void;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function MessageVisualEditor({
  fields,
  baseline,
  draft,
  guildOptions,
  onChange,
  onFocusField,
}: MessageVisualEditorProps) {
  if (!fields.length) {
    return <div className="osk-message-empty">Nenhum campo está disponível nesta área.</div>;
  }

  return <div className="osk-message-form">
    {fields.map((field) => {
      const changed = !valuesEqual(baseline[field.id], draft[field.id]);
      const currentText = typeof draft[field.id] === "string" ? String(draft[field.id]) : "";
      return <section key={field.id} className="osk-message-form__field" data-changed={changed || undefined} data-type={field.type} onFocusCapture={() => onFocusField?.(field)}>
        <header>
          <div><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div>
          {field.maxLength && ["text", "textarea", "url"].includes(field.type) && <span>{currentText.length}/{field.maxLength}</span>}
        </header>
        <DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} />
      </section>;
    })}
  </div>;
}
