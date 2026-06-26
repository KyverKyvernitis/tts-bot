import { ChevronLeft, ChevronRight, Settings } from "lucide-react";
import { useEffect, useState } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardSectionSummary,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import {
  DashboardFieldControl,
  displayDashboardValue,
} from "./DashboardFieldControl";
import { MessageEditor } from "./message-editor";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  summary: DashboardSectionSummary | undefined;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  hasUnsavedChanges: boolean;
  applying: boolean;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
  onApply(): void | Promise<void>;
  onMessageEditorActiveChange?(active: boolean): void;
  onBack(): void;
}

export function SectionEditor({
  section,
  module,
  summary,
  values,
  draft,
  guildOptions,
  hasUnsavedChanges,
  applying,
  onChange,
  onApply,
  onMessageEditorActiveChange,
  onBack,
}: SectionEditorProps) {
  void summary;
  const Icon = module?.icon ?? Settings;
  const groups = section.groups && section.groups.length > 0 ? section.groups : null;
  const [activeGroup, setActiveGroup] = useState<string | null>(null);

  useEffect(() => {
    setActiveGroup(null);
  }, [section.id]);

  const insideGroup = Boolean(groups) && activeGroup !== null;
  const fieldsToShow = groups ? section.fields.filter((field) => field.group === activeGroup) : section.fields;
  const activeGroupMetadata = activeGroup ? section.groupMetadata?.[activeGroup] : undefined;
  const isMessageGroup = insideGroup && activeGroupMetadata?.kind === "message";

  useEffect(() => {
    onMessageEditorActiveChange?.(isMessageGroup);
    return () => onMessageEditorActiveChange?.(false);
  }, [isMessageGroup, onMessageEditorActiveChange]);

  function handleBack() {
    if (insideGroup) {
      setActiveGroup(null);
      return;
    }
    onBack();
  }

  if (isMessageGroup && activeGroup) {
    return (
      <MessageEditor
        sectionId={section.id}
        sectionLabel={section.label}
        groupLabel={activeGroup}
        fields={fieldsToShow}
        values={values}
        draft={draft}
        guildOptions={guildOptions}
        variables={activeGroupMetadata?.variables}
        hasUnsavedChanges={hasUnsavedChanges}
        applying={applying}
        onChange={onChange}
        onApply={onApply}
        onBack={() => setActiveGroup(null)}
      />
    );
  }

  return (
    <section className="osk-page">
      <button className="osk-back-btn" onClick={handleBack}>
        <ChevronLeft size={14} />
        {insideGroup ? section.label : "Início"}
      </button>

      <div className="osk-section-head">
        <span className="osk-section-icon">
          <Icon size={22} />
        </span>
        {insideGroup ? (
          <div>
            <span className="osk-hero-eyebrow">{section.label}</span>
            <h1>{activeGroup}</h1>
          </div>
        ) : (
          <div>
            <h1>{section.label}</h1>
            <p>{module?.description ?? section.description}</p>
          </div>
        )}
      </div>

      {groups && !insideGroup ? (
        <div className="osk-module-grid">
          {groups.map((group, idx) => (
            <button
              key={group}
              className="osk-module-card"
              data-state="neutral"
              style={{ animationDelay: `${idx * 24}ms` }}
              onClick={() => setActiveGroup(group)}
            >
              <span className="osk-module-icon">
                <Icon size={20} />
              </span>
              <span className="osk-module-body">
                <span className="osk-module-head">
                  <strong>{group}</strong>
                </span>
              </span>
              <ChevronRight size={18} className="osk-module-chev" />
            </button>
          ))}
        </div>
      ) : (
        <div className="osk-fields">
          {fieldsToShow.map((field) => {
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

                <DashboardFieldControl
                  field={field}
                  value={draft[field.id]}
                  guildOptions={guildOptions}
                  onChange={onChange}
                />

                <span className="osk-field-hint">
                  Atual: <strong>{displayDashboardValue(field, values[field.id], guildOptions)}</strong>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
