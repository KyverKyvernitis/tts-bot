import { AlertTriangle, ChevronDown, LockKeyhole, Settings2, Users } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { DashboardFieldDefinition } from "../types/dashboard";

interface TicketPermissionsEditorProps {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}

type PermissionProfile = "private" | "collaborative" | "custom";

function scopeOf(field: DashboardFieldDefinition): "everyone" | "staff" | "creator" | "other" {
  if (field.id.includes(".everyone.")) return "everyone";
  if (field.id.includes(".staff.")) return "staff";
  if (field.id.includes(".creator.")) return "creator";
  return "other";
}

function expectedProfileValue(field: DashboardFieldDefinition, profile: Exclude<PermissionProfile, "custom">): boolean {
  const scope = scopeOf(field);
  if (scope === "everyone") return profile === "collaborative";
  if (scope === "staff") return true;
  if (scope === "creator") return !field.id.endsWith(".mention_everyone");
  return false;
}

function detectProfile(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): PermissionProfile {
  if (fields.length > 0 && fields.every((field) => Boolean(draft[field.id]) === expectedProfileValue(field, "private"))) return "private";
  if (fields.length > 0 && fields.every((field) => Boolean(draft[field.id]) === expectedProfileValue(field, "collaborative"))) return "collaborative";
  return "custom";
}

const PROFILES: Array<{ id: PermissionProfile; label: string; description: string; icon: typeof LockKeyhole }> = [
  { id: "private", label: "Privado", description: "Somente o autor e a equipe acessam o ticket.", icon: LockKeyhole },
  { id: "collaborative", label: "Colaborativo", description: "Os demais membros também podem acompanhar e responder.", icon: Users },
  { id: "custom", label: "Personalizado", description: "Controle individualmente cada permissão.", icon: Settings2 },
];

export function TicketPermissionsEditor({ fields, draft, renderFields, onChange }: TicketPermissionsEditorProps) {
  const detected = useMemo(() => detectProfile(fields, draft), [draft, fields]);
  const [advancedOpen, setAdvancedOpen] = useState(detected === "custom");

  function applyProfile(profile: PermissionProfile) {
    if (profile === "custom") {
      setAdvancedOpen(true);
      return;
    }
    for (const field of fields) {
      onChange(field, expectedProfileValue(field, profile));
    }
    setAdvancedOpen(false);
  }

  const staffView = fields.find((field) => field.id === "tickets.permissions.staff.view_channel");
  const creatorView = fields.find((field) => field.id === "tickets.permissions.creator.view_channel");
  const risky = (staffView && !Boolean(draft[staffView.id])) || (creatorView && !Boolean(draft[creatorView.id]));
  const grouped = [
    { id: "creator", label: "Autor do ticket", description: "Permissões da pessoa que abriu o atendimento." },
    { id: "staff", label: "Equipe responsável", description: "Permissões dos cargos de atendimento." },
    { id: "everyone", label: "Demais membros", description: "Permissões de quem não faz parte do atendimento." },
  ] as const;

  return <div className="osk-permission-editor">
    <div className="osk-permission-profiles">
      {PROFILES.map((profile) => {
        const Icon = profile.icon;
        const selected = detected === profile.id;
        return <button key={profile.id} type="button" data-selected={selected || undefined} onClick={() => applyProfile(profile.id)}>
          <span><Icon size={18} /></span>
          <strong>{profile.label}</strong>
          <small>{profile.description}</small>
        </button>;
      })}
    </div>

    {risky && <div className="osk-inline-warning"><AlertTriangle size={17} /><span><strong>Configuração arriscada</strong><small>O autor ou a equipe pode ficar sem acesso ao ticket. Revise as permissões antes de salvar.</small></span></div>}

    <button type="button" className="osk-permission-advanced-toggle" onClick={() => setAdvancedOpen((current) => !current)} aria-expanded={advancedOpen}>
      <span><Settings2 size={17} /><strong>Ajustes avançados</strong></span>
      <ChevronDown size={17} />
    </button>

    {advancedOpen && <div className="osk-permission-groups">
      {grouped.map((group) => {
        const groupFields = fields.filter((field) => scopeOf(field) === group.id).map((field) => ({
          ...field,
          label: field.label.replace(/^[^:]+:\s*/, ""),
        }));
        if (!groupFields.length) return null;
        return <section key={group.id}>
          <header><strong>{group.label}</strong><small>{group.description}</small></header>
          {renderFields(groupFields)}
        </section>;
      })}
    </div>}
  </div>;
}
