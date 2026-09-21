import { Eye, PencilLine } from "lucide-react";
import { useContext, useState } from "react";
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
  const senderIds = new Set(editor.senderFieldIds || []);
  return <article className="osk-message-preview-card" data-changed={changed || undefined} data-expanded={expanded || undefined}>
    <header><div><span className="osk-module-eyebrow">Prévia da mensagem</span><h3>{editor.label}</h3></div><button type="button" className="osk-preview-edit" onClick={() => onOpen(editor, variables)}><PencilLine size={15} />Editar</button></header>
    <button type="button" className="osk-preview-expand" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><Eye size={16} />{expanded ? "Recolher prévia" : "Mostrar prévia"}</button>
    <div className="osk-module-preview-body"><MessagePreview {...identity} sectionId={sectionId} editorId={editor.id} groupLabel={editor.label} presentation={editor.presentation || "generic"} fields={fields.filter(field => !senderIds.has(field.id))} senderFields={fields.filter(field => senderIds.has(field.id))} draft={draft} guildOptions={guildOptions} /></div>
    <footer>Prévia do rascunho · use Salvar no servidor para publicar os ajustes.</footer>
  </article>;
}
