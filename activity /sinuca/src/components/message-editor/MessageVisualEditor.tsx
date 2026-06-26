import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";

interface MessageVisualEditorProps {
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
}

export function MessageVisualEditor({
  fields,
  values,
  draft,
  guildOptions,
  onChange,
}: MessageVisualEditorProps) {
  return (
    <div className="osk-message-visual">
      {fields.map((field) => {
        const changed = draft[field.id] !== values[field.id];
        return (
          <div
            key={field.id}
            className="osk-message-field"
            data-type={field.type}
            data-changed={changed}
          >
            <div className="osk-message-field__head">
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
            <DashboardFieldControl
              field={field}
              value={draft[field.id]}
              guildOptions={guildOptions}
              onChange={onChange}
            />
          </div>
        );
      })}
    </div>
  );
}
