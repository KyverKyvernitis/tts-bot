import type { DashboardFieldDefinition } from "../../types/dashboard";

export function AccentControl({ field, selectedFieldId, onSelectField, label = "Editar cor de destaque" }: {
  field?: DashboardFieldDefinition;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  label?: string;
}) {
  if (!field || !onSelectField) return null;
  return (
    <button
      type="button"
      className="osk-message-preview__accent-control"
      data-selected={selectedFieldId === field.id || undefined}
      data-message-field-anchor={field.id}
      aria-label={label}
      onClick={(event) => { event.stopPropagation(); onSelectField(field); }}
    />
  );
}
