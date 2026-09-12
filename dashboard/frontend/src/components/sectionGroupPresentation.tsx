import {
  AlertTriangle,
  AudioLines,
  Bell,
  CalendarDays,
  FileText,
  Image,
  ListChecks,
  LockKeyhole,
  Mail,
  MessageSquare,
  Palette,
  Power,
  Route,
  Send,
  Settings,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Type,
  Users,
  Webhook,
  type LucideIcon,
} from "lucide-react";

const GROUP_DESCRIPTIONS: Record<string, string> = {
  "Ativação": "Controle quando esta função pode executar novas ações.",
  "Mensagem de entrada": "Canal, formato e conteúdo enviado quando alguém entra.",
  "Mensagem privada": "Mensagem enviada diretamente ao novo membro.",
  "Aparência": "Cores, imagem e detalhes visuais compartilhados.",
  "Cargos": "Cargos entregues automaticamente na entrada.",
  "Webhook": "Nome, avatar e canal usados no envio.",
  "Canais": "Escolha onde esta função será usada.",
  "Painel": "Conteúdo e aparência da mensagem pública.",
  "Perguntas": "Campos exibidos no formulário do Discord.",
  "Resposta": "Como as respostas chegam para a equipe.",
  "Aprovação": "Ações após aprovar ou rejeitar uma resposta.",
  "Atendimento": "Categoria, equipe e destinos do atendimento.",
  "Comportamento": "Limites e regras automáticas da função.",
  "Fluxos": "Tipos de atendimento e o que cada opção faz.",
  "Textos": "Mensagens usadas durante o atendimento.",
  "Denúncias": "Categorias disponíveis para denúncias.",
  "Permissões": "Acesso da equipe, do autor e dos demais membros.",
  "Mensagens": "Respostas mostradas ao aplicar ou remover cores.",
  "Cores": "Lista visual de cores e cargos vinculados.",
  "Geral": "Preferências principais desta função.",
  "Registro de datas": "Mensagens e regras usadas no cadastro.",
  "Avisos": "Horário e textos dos anúncios automáticos.",
  "Calendário": "Conteúdo do calendário de aniversários.",
  "Voz": "Mecanismo, idioma, voz e ritmo da leitura.",
  "Prefixos": "Caracteres que iniciam cada mecanismo de voz.",
};

export function sectionGroupDescription(group: string): string {
  return GROUP_DESCRIPTIONS[group] || "Ajustes desta função.";
}

export function sectionGroupIcon(group: string): LucideIcon {
  const normalized = group.toLocaleLowerCase("pt-BR");
  if (normalized.includes("ativação")) return Power;
  if (normalized.includes("webhook")) return Webhook;
  if (normalized.includes("mensagem privada")) return Mail;
  if (normalized.includes("mensagem") || normalized.includes("textos")) return MessageSquare;
  if (normalized.includes("aparência")) return Image;
  if (normalized.includes("cargo") || normalized.includes("permiss")) return ShieldCheck;
  if (normalized.includes("atendimento") || normalized.includes("canal")) return Users;
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
