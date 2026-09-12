import { Pencil, Settings2 } from "lucide-react";
import type { ReactNode } from "react";
import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { DiscordRichText } from "./DiscordRichText";
import { MessageInlineTextEditor } from "./MessageInlineTextEditor";
import { readableFieldLabel } from "./messageEditorUtils";
import { fieldString } from "./messagePreviewModel";

export function EditableRegion({
  field,
  interactive,
  selectedFieldId,
  onSelectField,
  onEditField,
  hasOptions = false,
  onOpenOptions,
  className,
  children,
  placeholder,
  textEditable = false,
}: {
  field?: DashboardFieldDefinition;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasOptions?: boolean;
  onOpenOptions?(field: DashboardFieldDefinition): void;
  className?: string;
  children?: ReactNode;
  placeholder?: string;
  textEditable?: boolean;
}) {
  if (!children && !interactive) return null;
  if (!interactive || !field || !onSelectField) return <div className={className}>{children}</div>;

  const selected = selectedFieldId === field.id;
  const selectOrEdit = () => {
    onSelectField(field);
    if (textEditable && onEditField) onEditField(field);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      className={className ? `osk-message-editable ${className}` : "osk-message-editable"}
      data-selected={selected || undefined}
      data-text-editable={textEditable || undefined}
      data-message-field-anchor={field.id}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        selectOrEdit();
      }}
      onDoubleClick={(event) => {
        if (!textEditable || !onEditField) return;
        event.preventDefault();
        event.stopPropagation();
        onSelectField(field);
        onEditField(field);
      }}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        selectOrEdit();
      }}
      title={textEditable ? `Editar ${field.label}` : `Configurar ${field.label}`}
    >
      {children ?? <span className="osk-message-preview__ghost">+ {placeholder ?? readableFieldLabel(field)}</span>}
      {selected && textEditable && <span className="osk-message-editable__pencil" aria-hidden="true"><Pencil size={11} /></span>}
      {selected && hasOptions && onOpenOptions && (
        <span
          role="button"
          tabIndex={0}
          className="osk-message-editable__options"
          data-with-pencil={textEditable || undefined}
          aria-label={`Abrir opções de ${field.label}`}
          title={`Opções de ${field.label}`}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onOpenOptions(field);
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            event.stopPropagation();
            onOpenOptions(field);
          }}
        >
          <Settings2 size={11} />
        </span>
      )}
    </div>
  );
}

export function FieldText({
  field,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  editingFieldId,
  textSelection,
  onSelectField,
  onEditField,
  hasFieldOptions,
  onOpenFieldOptions,
  onFinishEdit,
  onChange,
  onTextSelection,
  className,
  placeholder,
}: {
  field?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasFieldOptions?(field: DashboardFieldDefinition): boolean;
  onOpenFieldOptions?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  className?: string;
  placeholder?: string;
}) {
  const value = fieldString(field, draft);
  const editing = Boolean(field && editingFieldId === field.id && onChange && onTextSelection && onFinishEdit);

  if (editing && field) {
    return (
      <div className={className ? `osk-message-editable osk-message-editable--editing ${className}` : "osk-message-editable osk-message-editable--editing"} data-selected="true" data-message-field-anchor={field.id}>
        <MessageInlineTextEditor
          field={field}
          value={value}
          selection={textSelection?.fieldId === field.id ? textSelection : null}
          onChange={(next) => onChange!(field, next)}
          onSelection={(start, end) => onTextSelection!(field, start, end)}
          onFinish={onFinishEdit!}
        />
      </div>
    );
  }

  return (
    <EditableRegion
      field={field}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      onEditField={onEditField}
      hasOptions={Boolean(field && hasFieldOptions?.(field))}
      onOpenOptions={onOpenFieldOptions}
      className={className}
      placeholder={placeholder}
      textEditable={Boolean(field && (field.type === "text" || field.type === "textarea"))}
    >
      {value.trim() ? <DiscordRichText text={value} guildOptions={guildOptions} /> : undefined}
    </EditableRegion>
  );
}
