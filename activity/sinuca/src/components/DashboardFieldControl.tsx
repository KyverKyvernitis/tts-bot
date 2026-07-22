import { GripVertical, Plus, Trash2 } from "lucide-react";
import type {
  DashboardChannelOption,
  DashboardColorSlot,
  DashboardFieldDefinition,
  DashboardFormField,
  DashboardOptionsPayload,
} from "../types/dashboard";
import { SmartSelect, type SmartSelectOption } from "./SmartSelect";

interface DashboardFieldControlProps {
  field: DashboardFieldDefinition;
  value: unknown;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}

const TEXT_LIKE_CHANNEL_TYPES = new Set([0, 5, 15, 16]);
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
  return channels.filter((channel) => {
    if (kind === "category") return channel.type === CATEGORY_CHANNEL_TYPE;
    if (kind === "voice") return VOICE_CHANNEL_TYPES.has(channel.type);
    return TEXT_LIKE_CHANNEL_TYPES.has(channel.type);
  }).map((channel) => ({ value: channel.id, label: kind === "category" ? `📁 ${channel.name}` : kind === "voice" ? `🔊 ${channel.name}` : `# ${channel.name}` }));
}

export function stringifyDashboardValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(value) : "";
  return String(value);
}

export function displayDashboardValue(field: DashboardFieldDefinition, value: unknown, guildOptions: DashboardOptionsPayload | null): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if (field.type === "role_multi") return Array.isArray(value) && value.length ? `${value.length} cargo${value.length === 1 ? "" : "s"}` : "Nenhum cargo";
  if (field.type === "string_list") return Array.isArray(value) && value.length ? `${value.length} item${value.length === 1 ? "" : "s"}` : "Lista vazia";
  if (field.type === "form_fields") return Array.isArray(value) && value.length ? `${value.length} pergunta${value.length === 1 ? "" : "s"}` : "Nenhuma pergunta";
  if (field.type === "color_slots") return value && typeof value === "object" ? `${Object.keys(value as object).length} cores` : "Nenhuma cor";
  if (field.type === "channel" || field.type === "role") {
    const id = stringifyDashboardValue(value);
    if (!id || Number(id) <= 0) return "Não configurado";
    const list = field.type === "channel" ? guildOptions?.channels : guildOptions?.roles;
    const match = list?.find((item) => item.id === id);
    if (match) return field.type === "channel" ? `#${match.name}` : `@${match.name}`;
    return id;
  }
  if (field.type === "select") {
    const raw = stringifyDashboardValue(value);
    return (field.options?.find((item) => item.value === raw)?.label ?? raw) || "Não configurado";
  }
  const text = stringifyDashboardValue(value).trim();
  return text || "Não configurado";
}

export function DashboardFieldControl({ field, value, guildOptions, onChange }: DashboardFieldControlProps) {
  const currentValue = stringifyDashboardValue(value);
  const channelOptions = field.type === "channel" && guildOptions?.ok ? channelOptionsForField(field, guildOptions.channels) : null;
  const roleOptions = (field.type === "role" || field.type === "role_multi") && guildOptions?.ok
    ? guildOptions.roles.map((role) => ({ value: role.id, label: `@${role.name}` }))
    : null;

  if (field.type === "boolean") {
    return <label className="osk-switch"><input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(field, event.target.checked)} /><span className="osk-switch-track" /><span className="osk-switch-label">{Boolean(value) ? "Ligado" : "Desligado"}</span></label>;
  }

  if (field.type === "select") {
    return <SmartSelect id={`field-${field.id}`} value={currentValue} options={field.options ?? []} onChange={(next) => onChange(field, next)} placeholder="Selecione uma opção" />;
  }

  if (field.type === "channel" && channelOptions) {
    return <SmartSelect id={`field-${field.id}`} value={currentValue} options={channelOptions} onChange={(next) => onChange(field, next === currentValue ? "" : next)} placeholder="Selecione um canal" emptyLabel="Nenhum canal compatível encontrado" />;
  }

  if (field.type === "role" && roleOptions) {
    return <SmartSelect id={`field-${field.id}`} value={currentValue} options={roleOptions} onChange={(next) => onChange(field, next === currentValue ? "" : next)} placeholder="Selecione um cargo" emptyLabel="Nenhum cargo encontrado" />;
  }

  if (field.type === "role_multi") {
    return <RoleMultiEditor field={field} value={value} options={roleOptions ?? []} onChange={onChange} />;
  }

  if (field.type === "string_list") {
    const lines = Array.isArray(value) ? value.map(String).join("\n") : stringifyDashboardValue(value);
    return <textarea className="osk-list-textarea" value={lines} maxLength={field.maxLength} placeholder={field.placeholder || "Um item por linha"} onChange={(event) => onChange(field, event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))} />;
  }

  if (field.type === "form_fields") {
    return <FormFieldsEditor field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "color_slots") {
    return <ColorSlotsEditor field={field} value={value} roles={guildOptions?.ok ? guildOptions.roles.map((role) => ({ value: role.id, label: `@${role.name}` })) : []} onChange={onChange} />;
  }

  if (field.type === "textarea") {
    return <textarea value={currentValue} maxLength={field.maxLength} placeholder={field.placeholder} onChange={(event) => onChange(field, event.target.value)} />;
  }

  return <div className={field.type === "color" ? "osk-color-input" : undefined}>
    {field.type === "color" && <input type="color" value={/^#[0-9a-f]{6}$/i.test(currentValue) ? currentValue : "#5865f2"} onChange={(event) => onChange(field, event.target.value)} aria-label={field.label} />}
    <input
      type={field.type === "number" ? "number" : field.type === "url" ? "url" : "text"}
      min={field.min}
      max={field.max}
      maxLength={field.maxLength}
      value={currentValue}
      placeholder={field.placeholder ?? (field.type === "channel" ? "ID do canal" : field.type === "role" ? "ID do cargo" : field.type === "url" ? "https://..." : "")}
      onChange={(event) => onChange(field, event.target.value)}
    />
  </div>;
}

function RoleMultiEditor({ field, value, options, onChange }: { field: DashboardFieldDefinition; value: unknown; options: SmartSelectOption[]; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const selected = Array.isArray(value) ? value.map(String) : [];
  const toggle = (roleId: string) => onChange(field, selected.includes(roleId) ? selected.filter((id) => id !== roleId) : [...selected, roleId]);
  if (!options.length) return <input value={selected.join(", ")} placeholder="IDs separados por vírgula" onChange={(event) => onChange(field, event.target.value.split(/[\s,]+/).filter(Boolean))} />;
  return <div className="osk-multi-options">{options.map((option) => <label key={option.value} data-selected={selected.includes(option.value) || undefined}><input type="checkbox" checked={selected.includes(option.value)} onChange={() => toggle(option.value)} /><span>{option.label}</span></label>)}</div>;
}

function FormFieldsEditor({ field, value, onChange }: { field: DashboardFieldDefinition; value: unknown; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const fields = Array.isArray(value) ? value.map((item, index) => normalizeFormField(item, index)) : [];
  const update = (index: number, patch: Partial<DashboardFormField>) => onChange(field, fields.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  const remove = (index: number) => onChange(field, fields.filter((_, itemIndex) => itemIndex !== index));
  const add = () => {
    if (fields.length >= 5) return;
    onChange(field, [...fields, normalizeFormField({}, fields.length)]);
  };
  return <div className="osk-form-fields-editor">
    {fields.map((item, index) => <article key={item.id || index}>
      <header><span><GripVertical size={16} /> Pergunta {index + 1}</span><button type="button" onClick={() => remove(index)} aria-label="Remover pergunta"><Trash2 size={15} /></button></header>
      <div className="osk-inline-grid"><label><span>Rótulo</span><input value={item.label} maxLength={45} onChange={(event) => update(index, { label: event.target.value })} /></label><label><span>Nome na resposta</span><input value={item.response_label} maxLength={80} onChange={(event) => update(index, { response_label: event.target.value })} /></label></div>
      <label><span>Exemplo ou instrução</span><input value={item.placeholder} maxLength={100} onChange={(event) => update(index, { placeholder: event.target.value })} /></label>
      <div className="osk-check-row"><label><input type="checkbox" checked={item.enabled} onChange={(event) => update(index, { enabled: event.target.checked })} />Ativa</label><label><input type="checkbox" checked={item.required} onChange={(event) => update(index, { required: event.target.checked })} />Obrigatória</label><label><input type="checkbox" checked={item.long} onChange={(event) => update(index, { long: event.target.checked })} />Texto longo</label><label><input type="checkbox" checked={item.show_in_response} onChange={(event) => update(index, { show_in_response: event.target.checked })} />Mostrar na resposta</label></div>
    </article>)}
    <button type="button" className="osk-add-row" onClick={add} disabled={fields.length >= 5}><Plus size={15} />Adicionar pergunta ({fields.length}/5)</button>
  </div>;
}

function normalizeFormField(value: unknown, index: number): DashboardFormField {
  const raw = value && typeof value === "object" ? value as Partial<DashboardFormField> : {};
  return {
    id: String(raw.id || `field${index + 1}`), label: String(raw.label || `Pergunta ${index + 1}`), placeholder: String(raw.placeholder || ""),
    response_label: String(raw.response_label || raw.label || `Pergunta ${index + 1}`), required: raw.required !== false, long: Boolean(raw.long),
    show_in_response: raw.show_in_response !== false, enabled: raw.enabled !== false, min_length: Number(raw.min_length || 0), max_length: Number(raw.max_length || (raw.long ? 1000 : 120)),
  };
}

function ColorSlotsEditor({ field, value, roles, onChange }: { field: DashboardFieldDefinition; value: unknown; roles: SmartSelectOption[]; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const slots = value && typeof value === "object" ? value as Record<string, DashboardColorSlot> : {};
  const ordered = Object.entries(slots).sort(([a], [b]) => Number(a) - Number(b));
  const update = (key: string, patch: Partial<DashboardColorSlot>) => onChange(field, { ...slots, [key]: { ...slots[key], ...patch } });
  return <div className="osk-color-slots-editor">{ordered.map(([key, slot]) => {
    const color = /^#[0-9a-f]{6}$/i.test(String(slot.role_hex || "")) ? String(slot.role_hex) : "#5865f2";
    return <article key={key}>
      <span className="osk-color-slot-swatch" style={{ background: color }}><b>{slot.number || key}</b></span>
      <label><span>Nome</span><input value={String(slot.name || "")} maxLength={40} onChange={(event) => update(key, { name: event.target.value, role_name: event.target.value, managed: false })} /></label>
      <label><span>Cor</span><span className="osk-color-slot-color"><input type="color" value={color} onChange={(event) => update(key, { role_hex: event.target.value, text_hex: event.target.value, managed: false })} /><input value={color} onChange={(event) => update(key, { role_hex: event.target.value, text_hex: event.target.value, managed: false })} /></span></label>
      <label className="osk-color-slot-role"><span>Cargo</span>{roles.length ? <SmartSelect id={`color-slot-${key}`} value={String(slot.role_id || "")} options={roles} onChange={(next) => update(key, { role_id: next, managed: false })} placeholder="Selecione o cargo" /> : <input value={String(slot.role_id || "")} placeholder="ID do cargo" onChange={(event) => update(key, { role_id: event.target.value, managed: false })} />}</label>
    </article>;
  })}</div>;
}
