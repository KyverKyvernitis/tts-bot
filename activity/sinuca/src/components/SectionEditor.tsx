import {
  AlertTriangle,
  ArrowLeft,
  AudioLines,
  Bell,
  CalendarDays,
  ChevronDown,
  FileText,
  Image,
  ListChecks,
  LockKeyhole,
  Mail,
  MessageSquare,
  Palette,
  PencilLine,
  Route,
  Send,
  Settings,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Type,
  Users,
  Webhook,
} from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { DashboardFieldControl, displayDashboardValue } from "./DashboardFieldControl";
import { MessageEditor } from "./message-editor";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  previewBotName?: string;
  previewBotAvatarUrl?: string | null;
  hasUnsavedChanges: boolean;
  applying: boolean;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onApply(): void | Promise<void>;
  onMessageEditorActiveChange?(active: boolean): void;
  onBack(): void;
}

const GROUP_DESCRIPTIONS: Record<string, string> = {
  "Mensagem de entrada": "Canal, formato e conteúdo enviado quando alguém entra.",
  "Embed": "Aparência e conteúdo do embed público.",
  "Mensagem privada": "Mensagem enviada diretamente ao novo membro.",
  "Aparência": "Cores, imagem e detalhes visuais.",
  "Cargos": "Cargos entregues automaticamente na entrada.",
  "Webhook": "Nome, avatar e canal usados no envio.",
  "Canais": "Escolha onde esta função será usada.",
  "Painel": "Texto e aparência da mensagem pública.",
  "Perguntas": "Campos exibidos no formulário.",
  "Resposta": "Como as respostas chegam para a equipe.",
  "Aprovação": "Ações após aprovar ou rejeitar uma resposta.",
  "Canais e cargos": "Categoria, equipe e destinos do atendimento.",
  "Comportamento": "Limites e regras automáticas da função.",
  "Fluxos": "Tipos de atendimento e o que cada opção faz.",
  "Textos": "Mensagens usadas durante o atendimento.",
  "Denúncias": "Categorias disponíveis para denúncias.",
  "Permissões": "Acesso da equipe, do autor e dos demais membros.",
  "Mensagens": "Respostas mostradas ao aplicar ou remover cores.",
  "Cores": "Lista visual de cores e cargos vinculados.",
  "Geral": "Ativação e preferências principais.",
  "Registro de datas": "Mensagens e regras usadas no cadastro.",
  "Avisos": "Horário e textos dos anúncios automáticos.",
  "Calendário": "Conteúdo do calendário de aniversários.",
  "Voz": "Mecanismo, idioma, voz e ritmo da leitura.",
  "Prefixos": "Caracteres que iniciam cada mecanismo de voz.",
};

function groupIcon(group: string) {
  const normalized = group.toLocaleLowerCase("pt-BR");
  if (normalized.includes("webhook")) return Webhook;
  if (normalized.includes("mensagem privada")) return Mail;
  if (normalized.includes("mensagem") || normalized.includes("textos")) return MessageSquare;
  if (normalized.includes("embed") || normalized.includes("aparência")) return Image;
  if (normalized.includes("cargo") || normalized.includes("permiss")) return ShieldCheck;
  if (normalized.includes("canal")) return Users;
  if (normalized.includes("painel")) return FileText;
  if (normalized.includes("pergunta")) return ListChecks;
  if (normalized.includes("resposta")) return Send;
  if (normalized.includes("aprovação")) return LockKeyhole;
  if (normalized.includes("fluxo")) return Route;
  if (normalized.includes("denúncia")) return AlertTriangle;
  if (normalized.includes("cor")) return Palette;
  if (normalized.includes("registro")) return CalendarDays;
  if (normalized.includes("aviso")) return Bell;
  if (normalized.includes("calendário")) return CalendarDays;
  if (normalized.includes("voz")) return AudioLines;
  if (normalized.includes("prefix")) return Type;
  if (normalized.includes("comport")) return SlidersHorizontal;
  if (normalized.includes("geral")) return Settings2;
  return Settings;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function SectionEditor({
  section, module, values, draft, guildOptions, previewBotName, previewBotAvatarUrl,
  hasUnsavedChanges, applying, onChange, onApply, onMessageEditorActiveChange, onBack,
}: SectionEditorProps) {
  const Icon = module?.icon ?? Settings;
  const groups = section.groups?.length ? section.groups : null;
  const [openGroups, setOpenGroups] = useState<string[]>([]);
  const [messageGroup, setMessageGroup] = useState<string | null>(null);

  useEffect(() => {
    setOpenGroups(groups?.[0] ? [groups[0]] : []);
    setMessageGroup(null);
  }, [section.id, groups]);

  const messageFields = useMemo(
    () => messageGroup ? section.fields.filter((field) => field.group === messageGroup) : [],
    [messageGroup, section.fields],
  );
  const messageMetadata = messageGroup ? section.groupMetadata?.[messageGroup] : undefined;

  useEffect(() => {
    onMessageEditorActiveChange?.(Boolean(messageGroup));
    return () => onMessageEditorActiveChange?.(false);
  }, [messageGroup, onMessageEditorActiveChange]);

  if (messageGroup) {
    return <MessageEditor
      sectionId={section.id}
      sectionLabel={section.label}
      groupLabel={messageGroup}
      fields={messageFields}
      values={values}
      draft={draft}
      guildOptions={guildOptions}
      botName={previewBotName}
      botAvatarUrl={previewBotAvatarUrl}
      variables={messageMetadata?.variables}
      hasUnsavedChanges={hasUnsavedChanges}
      applying={applying}
      onChange={onChange}
      onApply={onApply}
      onBack={() => setMessageGroup(null)}
    />;
  }

  const toggleGroup = (group: string) => {
    setOpenGroups((current) => current.includes(group) ? current.filter((item) => item !== group) : [...current, group]);
  };

  return <section className="osk-dashboard-page osk-section-page">
    <button className="osk-page-back" onClick={onBack}><ArrowLeft size={16} />Funções</button>
    <header className="osk-function-heading">
      <span className="osk-function-heading-icon"><Icon size={24} /></span>
      <div><h1>{section.label}</h1><p>{module?.description || section.description}</p></div>
    </header>

    {groups ? (
      <div className="osk-accordion-list">
        {groups.map((group, index) => {
          const GroupIcon = groupIcon(group);
          const groupFields = section.fields.filter((field) => field.group === group);
          const changed = groupFields.filter((field) => !valuesEqual(values[field.id], draft[field.id])).length;
          const open = openGroups.includes(group);
          const isMessageGroup = section.groupMetadata?.[group]?.kind === "message";
          return <article key={group} className="osk-accordion" data-open={open || undefined} style={{ "--osk-card-index": index } as CSSProperties}>
            <button type="button" className="osk-accordion-trigger" onClick={() => toggleGroup(group)} aria-expanded={open}>
              <span className="osk-accordion-icon"><GroupIcon size={19} /></span>
              <span className="osk-accordion-copy"><strong>{group}</strong><small>{GROUP_DESCRIPTIONS[group] || "Ajustes desta função."}</small></span>
              {changed > 0 && <em>{changed} pendente{changed === 1 ? "" : "s"}</em>}
              <ChevronDown size={18} className="osk-accordion-chevron" />
            </button>
            <div className="osk-accordion-panel" aria-hidden={!open}>
              <div className="osk-accordion-panel-inner">
                {isMessageGroup ? (
                  <div className="osk-message-group-launcher">
                    <div><PencilLine size={19} /><span><strong>Editor visual</strong><small>Edite conteúdo, embed, variáveis e confira a prévia.</small></span></div>
                    <button type="button" className="osk-primary-button osk-primary-button--small" onClick={() => setMessageGroup(group)}>Editar mensagem</button>
                  </div>
                ) : (
                  <FieldsPanel sectionId={section.id} fields={groupFields} values={values} draft={draft} guildOptions={guildOptions} onChange={onChange} />
                )}
              </div>
            </div>
          </article>;
        })}
      </div>
    ) : (
      <div className="osk-settings-panel">
        <FieldsPanel sectionId={section.id} fields={section.fields} values={values} draft={draft} guildOptions={guildOptions} onChange={onChange} />
      </div>
    )}
  </section>;
}

function FieldsPanel({
  sectionId,
  fields,
  values,
  draft,
  guildOptions,
  onChange,
}: {
  sectionId: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}) {
  const engine = String(draft["tts.engine"] || "edge");
  const ignoredEnabledField = fields.find((field) => field.id === "tts.ignored_tts_role_enabled");

  const visibleFields = fields.filter((field) => {
    if (field.id === "tts.ignored_tts_role_enabled") return false;
    if (sectionId === "tts" && field.id === "tts.language") return engine === "gtts";
    if (sectionId === "tts" && ["tts.voice", "tts.rate", "tts.pitch"].includes(field.id)) return engine === "edge";
    return true;
  });

  return <div className="osk-compact-fields">
    {visibleFields.map((field) => {
      const isIgnoredRole = field.id === "tts.ignored_tts_role_id" && Boolean(ignoredEnabledField);
      const changed = !valuesEqual(draft[field.id], values[field.id])
        || (isIgnoredRole && !valuesEqual(draft[ignoredEnabledField!.id], values[ignoredEnabledField!.id]));
      const isComplex = ["textarea", "role_multi", "string_list", "form_fields", "color_slots"].includes(field.type);
      const displayField = isIgnoredRole
        ? { ...field, label: "Ignorar mensagens deste cargo", description: "Selecione Nenhum para desativar esta regra." }
        : field;
      const controlValue = isIgnoredRole && !Boolean(draft[ignoredEnabledField!.id]) ? "" : draft[field.id];
      const handleChange = (changedField: DashboardFieldDefinition, raw: unknown) => {
        onChange(changedField, raw);
        if (changedField.id === "tts.ignored_tts_role_id" && ignoredEnabledField) onChange(ignoredEnabledField, Boolean(String(raw || "").trim()));
      };

      return <div key={field.id} className="osk-compact-field" data-changed={changed || undefined} data-complex={isComplex || undefined}>
        <div className="osk-compact-field-copy">
          <strong>{displayField.label}</strong>
          {displayField.description && <small>{displayField.description}</small>}
          {changed && <span>Alterado · antes: {displayDashboardValue(field, values[field.id], guildOptions)}</span>}
        </div>
        <div className="osk-compact-field-control"><DashboardFieldControl field={displayField} value={controlValue} guildOptions={guildOptions} onChange={handleChange} /></div>
      </div>;
    })}
  </div>;
}
