import { ChevronLeft, Settings } from "lucide-react";
import type {
  DashboardChannelOption,
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardSectionSummary,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { SmartSelect, type SmartSelectOption } from "./SmartSelect";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  summary: DashboardSectionSummary | undefined;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
  onBack(): void;
}

// Tipos de canal do Discord: https://discord.com/developers/docs/resources/channel#channel-object-channel-types
const TEXT_LIKE_CHANNEL_TYPES = new Set([0, 5, 15]);
const VOICE_CHANNEL_TYPES = new Set([2, 13]);
const CATEGORY_CHANNEL_TYPE = 4;

function channelKindForField(field: DashboardFieldDefinition): "category" | "voice" | "text" {
  const hint = `${field.id} ${field.path}`.toLowerCase();
  if (hint.includes("category")) return "category";
  if (hint.includes("voice")) return "voice";
  return "text";
}

function channelOptionsForField(field: DashboardFieldDefinition, channels: DashboardChannelOption[]): SmartSelectOption[] {
  const kind = channelKindForField(field);
  const filtered = channels.filter((channel) => {
    if (kind === "category") return channel.type === CATEGORY_CHANNEL_TYPE;
    if (kind === "voice") return VOICE_CHANNEL_TYPES.has(channel.type);
    return TEXT_LIKE_CHANNEL_TYPES.has(channel.type);
  });
  return filtered.map((channel) => ({
    value: channel.id,
    label: kind === "category" ? channel.name : kind === "voice" ? `🔊 ${channel.name}` : `#${channel.name}`,
  }));
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(value) : "";
  return String(value);
}

function displayValue(field: DashboardFieldDefinition, value: unknown, guildOptions: DashboardOptionsPayload | null): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if (field.type === "channel" || field.type === "role") {
    const id = stringifyValue(value);
    if (!id || Number(id) <= 0) return "Não configurado";
    const list = field.type === "channel" ? guildOptions?.channels : guildOptions?.roles;
    const match = list?.find((item) => item.id === id);
    if (match) return field.type === "channel" ? `#${match.name}` : `@${match.name}`;
    return field.type === "channel" ? `#${id}` : `@${id}`;
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
  guildOptions,
  onChange,
  onBack,
}: SectionEditorProps) {
  const Icon = module?.icon ?? Settings;
  const optionsMissingReason = guildOptions && !guildOptions.ok ? guildOptions.error : null;

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
        <span className="osk-badge" data-state="neutral">
          {section.fields.length} opções
        </span>
      </div>

      <div className="osk-fields">
        {section.fields.map((field) => {
          const current = draft[field.id];
          const currentValue = stringifyValue(current);
          const changed = draft[field.id] !== values[field.id];
          const channelOptions = field.type === "channel" && guildOptions?.ok
            ? channelOptionsForField(field, guildOptions.channels)
            : null;
          const roleOptions = field.type === "role" && guildOptions?.ok
            ? guildOptions.roles.map((role) => ({ value: role.id, label: `@${role.name}` }))
            : null;

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
                <SmartSelect
                  id={`field-${field.id}`}
                  value={currentValue}
                  options={field.options ?? []}
                  onChange={(next) => onChange(field, next)}
                  placeholder="Selecione"
                />
              ) : field.type === "channel" && channelOptions ? (
                <SmartSelect
                  id={`field-${field.id}`}
                  value={currentValue}
                  options={channelOptions}
                  onChange={(next) => onChange(field, next === currentValue ? "" : next)}
                  placeholder="Selecione um canal"
                  emptyLabel="Nenhum canal encontrado"
                />
              ) : field.type === "role" && roleOptions ? (
                <SmartSelect
                  id={`field-${field.id}`}
                  value={currentValue}
                  options={roleOptions}
                  onChange={(next) => onChange(field, next === currentValue ? "" : next)}
                  placeholder="Selecione um cargo"
                  emptyLabel="Nenhum cargo encontrado"
                />
              ) : field.type === "textarea" ? (
                <textarea
                  value={currentValue}
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
                  value={currentValue}
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

              {(field.type === "channel" || field.type === "role") && !channelOptions && !roleOptions && optionsMissingReason && (
                <small className="osk-field-warn">
                  Lista de {field.type === "channel" ? "canais" : "cargos"} indisponível agora
                  (endpoint /dashboard/guild/:guildId/options — {optionsMissingReason}). Usando ID manual por enquanto.
                </small>
              )}

              <span className="osk-field-hint">
                Atual: <strong>{displayValue(field, values[field.id], guildOptions)}</strong>
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
