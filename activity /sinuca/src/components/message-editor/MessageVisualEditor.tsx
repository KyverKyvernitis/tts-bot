import { useEffect, useMemo, useState } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { MessagePreview } from "./MessagePreview";
import { readableFieldLabel } from "./messageEditorUtils";

interface MessageVisualEditorProps {
  groupLabel: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
}

function preferredInitialField(fields: DashboardFieldDefinition[]): DashboardFieldDefinition | undefined {
  return fields.find((field) => field.id.endsWith(".embed.description"))
    ?? fields.find((field) => field.id.endsWith(".embed.content"))
    ?? fields.find((field) => field.type === "textarea")
    ?? fields.find((field) => field.type === "text")
    ?? fields[0];
}

export function MessageVisualEditor({
  groupLabel,
  fields,
  values,
  draft,
  guildOptions,
  botName,
  botAvatarUrl,
  onChange,
}: MessageVisualEditorProps) {
  const initialField = useMemo(() => preferredInitialField(fields), [fields]);
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(initialField?.id ?? null);

  useEffect(() => {
    setSelectedFieldId(preferredInitialField(fields)?.id ?? null);
  }, [fields]);

  const selectedField = fields.find((field) => field.id === selectedFieldId) ?? initialField;
  const changed = selectedField ? draft[selectedField.id] !== values[selectedField.id] : false;

  return (
    <div className="osk-message-visual">
      <div className="osk-message-visual__canvas">
        <MessagePreview
          groupLabel={groupLabel}
          fields={fields}
          draft={draft}
          guildOptions={guildOptions}
          botName={botName}
          botAvatarUrl={botAvatarUrl}
          interactive
          selectedFieldId={selectedField?.id ?? null}
          onSelectField={(field) => setSelectedFieldId(field.id)}
        />
      </div>

      <div className="osk-message-visual__quick" aria-label="Campos da mensagem">
        {fields.map((field) => (
          <button
            key={field.id}
            type="button"
            data-active={selectedField?.id === field.id}
            data-changed={draft[field.id] !== values[field.id]}
            onClick={() => setSelectedFieldId(field.id)}
          >
            {readableFieldLabel(field)}
          </button>
        ))}
      </div>

      {selectedField ? (
        <section className="osk-message-visual__editor" data-changed={changed}>
          <div className="osk-message-visual__editor-head">
            <div>
              <small>Editando campo</small>
              <strong>{selectedField.label}</strong>
              {selectedField.description && <p>{selectedField.description}</p>}
            </div>
            {changed && <span className="osk-badge" data-state="changed">alterado</span>}
          </div>
          <DashboardFieldControl
            field={selectedField}
            value={draft[selectedField.id]}
            guildOptions={guildOptions}
            onChange={onChange}
          />
        </section>
      ) : (
        <div className="osk-message-empty">Toque em uma parte da mensagem para editar.</div>
      )}
    </div>
  );
}
