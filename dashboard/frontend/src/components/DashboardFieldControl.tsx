import {
  channelOptionsForField,
  dashboardBooleanStateLabel,
  channelOptionsWithCurrentValue,
  roleOptionsWithCurrentValue,
  displayDashboardValue,
  stringifyDashboardValue,
} from "../app/dashboardFieldValues";
export {
  channelOptionsForField,
  dashboardBooleanStateLabel,
  displayDashboardValue,
  stringifyDashboardValue,
} from "../app/dashboardFieldValues";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../types/dashboard";
import { SmartSelect } from "./SmartSelect";
import { RoleMultiEditor } from "./dashboard-fields/RoleMultiEditor";
import { StringListEditor } from "./dashboard-fields/StringListEditor";
import { FormFieldsEditor } from "./dashboard-fields/FormFieldsEditor";
import { ColorSlotsEditor } from "./dashboard-fields/ColorSlotsEditor";

interface DashboardFieldControlProps {
  field: DashboardFieldDefinition;
  value: unknown;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  selectedColorSlot?: number | null;
  colorSlotIds?: number[] | null;
  onColorSlotSelect?(slotNumber: number): void;
}

export function DashboardFieldControl({ field, value, guildOptions, onChange, onTextSelection, selectedColorSlot, colorSlotIds, onColorSlotSelect }: DashboardFieldControlProps) {
  const currentValue = stringifyDashboardValue(value);
  const channelOptions = field.type === "channel" && guildOptions?.ok
    ? channelOptionsWithCurrentValue(field, currentValue, guildOptions.channels)
    : null;
  const roleOptions = (field.type === "role" || field.type === "role_multi") && guildOptions?.ok
    ? roleOptionsWithCurrentValue(field, value, currentValue, guildOptions.roles)
    : null;

  if (field.type === "boolean") {
    const stateLabel = dashboardBooleanStateLabel(field.id, value);
    return <label className="osk-switch"><input type="checkbox" aria-label={field.label} checked={Boolean(value)} onChange={(event) => onChange(field, event.target.checked)} /><span className="osk-switch-track" /><span className="osk-switch-state">{stateLabel}</span></label>;
  }

  if (field.id === "tts.rate") {
    const numeric = Math.max(-100, Math.min(100, Number.parseInt(currentValue, 10) || 0));
    return <div className="osk-range-control"><input type="range" aria-label={field.label} min={-100} max={100} step={5} value={numeric} onChange={(event) => { const next = Number(event.target.value); onChange(field, `${next >= 0 ? "+" : ""}${next}%`); }} /><output>{numeric >= 0 ? "+" : ""}{numeric}%</output></div>;
  }

  if (field.id === "tts.pitch") {
    const numeric = Math.max(-100, Math.min(100, Number.parseInt(currentValue, 10) || 0));
    return <div className="osk-range-control"><input type="range" aria-label={field.label} min={-100} max={100} step={5} value={numeric} onChange={(event) => { const next = Number(event.target.value); onChange(field, `${next >= 0 ? "+" : ""}${next}Hz`); }} /><output>{numeric >= 0 ? "+" : ""}{numeric}Hz</output></div>;
  }

  if (field.type === "select") {
    const configuredOptions = field.options ?? [];
    const options = currentValue && !configuredOptions.some((option) => option.value === currentValue)
      ? [{ value: currentValue, label: `${currentValue} — valor atual` }, ...configuredOptions]
      : configuredOptions;
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={options} onChange={(next) => onChange(field, next)} placeholder="Selecione uma opção" />;
  }

  if (field.type === "channel" && channelOptions) {
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={[{ value: "", label: "Nenhum" }, ...channelOptions]} onChange={(next) => onChange(field, next)} placeholder="Selecione um canal" emptyLabel="Nenhum canal compatível encontrado" />;
  }

  if (field.type === "role" && roleOptions) {
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={[{ value: "", label: "Nenhum" }, ...roleOptions]} onChange={(next) => onChange(field, next)} placeholder="Selecione um cargo" emptyLabel="Nenhum cargo encontrado" />;
  }

  if (field.type === "role_multi") {
    return <RoleMultiEditor field={field} value={value} options={roleOptions ?? []} onChange={onChange} />;
  }

  if (field.type === "string_list") {
    return <StringListEditor field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "form_fields") {
    return <FormFieldsEditor field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "color_slots") {
    return <ColorSlotsEditor
      field={field}
      value={value}
      roles={guildOptions?.ok ? guildOptions.roles : []}
      selectedSlot={selectedColorSlot}
      visibleSlotIds={colorSlotIds}
      onSelectSlot={onColorSlotSelect}
      onChange={onChange}
    />;
  }

  if (field.type === "textarea") {
    return <textarea
      aria-label={field.label}
      data-message-field-id={field.id}
      value={currentValue}
      maxLength={field.maxLength}
      placeholder={field.placeholder}
      rows={Math.min(8, Math.max(3, currentValue.split("\n").length + 1))}
      onFocus={(event) => onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd)}
      onSelect={(event) => onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd)}
      onChange={(event) => {
        onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd);
        onChange(field, event.target.value);
      }}
    />;
  }

  const suffix = field.id === "tts.speech_limit_seconds" ? "segundos" : null;
  return <div className={`${field.type === "color" ? "osk-color-input" : ""}${suffix ? " osk-input-suffix" : ""}`.trim()}>
    {field.type === "color" && <input type="color" value={/^#[0-9a-f]{6}$/i.test(currentValue) ? currentValue : "#5865f2"} onChange={(event) => onChange(field, event.target.value)} aria-label={field.label} />}
    <input
      aria-label={field.label}
      data-message-field-id={field.type === "text" ? field.id : undefined}
      type={field.type === "number" ? "number" : field.type === "url" ? "url" : "text"}
      min={field.min}
      max={field.max}
      maxLength={field.maxLength}
      value={currentValue}
      placeholder={field.placeholder ?? (field.type === "channel" ? "ID do canal" : field.type === "role" ? "ID do cargo" : field.type === "url" ? "https://..." : "")}
      onFocus={field.type === "text" ? (event) => onTextSelection?.(field, event.currentTarget.selectionStart ?? currentValue.length, event.currentTarget.selectionEnd ?? currentValue.length) : undefined}
      onSelect={field.type === "text" ? (event) => onTextSelection?.(field, event.currentTarget.selectionStart ?? currentValue.length, event.currentTarget.selectionEnd ?? currentValue.length) : undefined}
      onChange={(event) => {
        if (field.type === "text") onTextSelection?.(field, event.currentTarget.selectionStart ?? event.currentTarget.value.length, event.currentTarget.selectionEnd ?? event.currentTarget.value.length);
        onChange(field, event.target.value);
      }}
    />
    {suffix && <span>{suffix}</span>}
  </div>;
}
