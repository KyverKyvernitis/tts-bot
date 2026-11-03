import { ChevronDown, Eye, PencilLine } from "lucide-react";
import { useContext, useId, useState } from "react";
import type { DashboardFieldDefinition, DashboardMessageEditorDefinition, DashboardOptionsPayload, DashboardTemplateVariables } from "../../types/dashboard";
import { MessagePreview } from "../message-editor/MessagePreview";
import { ModulePreviewContext } from "./ModulePreviewContext";

interface Props {
  sectionId: string; editor: DashboardMessageEditorDefinition; fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>; guildOptions: DashboardOptionsPayload | null; changed: boolean;
  variables?: DashboardTemplateVariables; onOpen(editor: DashboardMessageEditorDefinition, variables?: DashboardTemplateVariables): void;
}
export function MessagePreviewCard({ sectionId, editor, fields, draft, guildOptions, changed, variables, onOpen }: Props) {
  const identity = useContext(ModulePreviewContext);
  const [expanded, setExpanded] = useState(false);
  const previewId = useId();
  const senderIds = new Set(editor.senderFieldIds || []);
  return <article className="osk-message-preview-card" data-changed={changed || undefined} data-expanded={expanded || undefined}>
    <header><div><h3>{editor.label}</h3>{changed && <span className="osk-preview-changed" role="status">Alterada</span>}</div><button type="button" className="osk-preview-edit" aria-label={`Editar mensagem: ${editor.label}`} title={`Editar mensagem: ${editor.label}`} onClick={() => onOpen(editor, variables)}><PencilLine size={16} aria-hidden="true" />Editar</button></header>
    <button type="button" className="osk-preview-expand" aria-expanded={expanded} aria-controls={previewId} onClick={() => setExpanded(!expanded)}><Eye size={16} aria-hidden="true" /><span>{expanded ? "Recolher prévia" : "Mostrar prévia"}</span><ChevronDown size={16} aria-hidden="true" /></button>
    <div className="osk-module-preview-body" id={previewId} hidden={!expanded}><MessagePreview {...identity} sectionId={sectionId} editorId={editor.id} groupLabel={editor.label} presentation={editor.presentation || "generic"} fields={fields.filter(field => !senderIds.has(field.id))} senderFields={fields.filter(field => senderIds.has(field.id))} draft={draft} guildOptions={guildOptions} /></div>
  </article>;
}
