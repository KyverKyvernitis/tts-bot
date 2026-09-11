import { defaultFormsConfig } from "./dashboardFormsDefaults.js";
import { isPlainObject } from "./dashboardObjectUtils.js";

export function normalizeFormFields(raw: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(raw)) return defaultFormsConfig().modal.fields;
  return raw.slice(0, 5).map((item, index) => {
    const value = isPlainObject(item) ? item : {};
    const long = Boolean(value.long);
    const maxAllowed = long ? 1000 : 120;
    const minLength = Math.max(0, Math.min(maxAllowed, Math.trunc(Number(value.min_length) || 0)));
    const maxLength = Math.max(Math.max(1, minLength), Math.min(maxAllowed, Math.trunc(Number(value.max_length) || maxAllowed)));
    return {
      id: `field${index + 1}`,
      label: String(value.label || `Pergunta ${index + 1}`).trim().slice(0, 45) || `Pergunta ${index + 1}`,
      placeholder: String(value.placeholder || "").slice(0, 100),
      response_label: String(value.response_label || value.label || `Pergunta ${index + 1}`).trim().slice(0, 45),
      required: value.required !== false,
      long,
      show_in_response: value.show_in_response !== false,
      enabled: value.enabled !== false,
      min_length: minLength,
      max_length: maxLength,
    };
  });
}
