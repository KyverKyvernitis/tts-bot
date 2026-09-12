import type {
  DashboardFieldDefinition,
  DashboardMessageEditorPresentation,
  DashboardOptionsPayload,
} from "../../types/dashboard";

export interface MessagePreviewProps {
  sectionId?: string;
  editorId?: string;
  groupLabel: string;
  presentation?: DashboardMessageEditorPresentation;
  fields: DashboardFieldDefinition[];
  senderFields?: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
  interactive?: boolean;
  senderSelected?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  selectedColorSlot?: number | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectSender?(): void;
  onEditSender?(): void;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasFieldOptions?(field: DashboardFieldDefinition): boolean;
  onOpenFieldOptions?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  onSelectColorSlot?(slotNumber: number, openInspector?: boolean): void;
}

export type PreviewCoreProps = Omit<
  MessagePreviewProps,
  | "groupLabel"
  | "botName"
  | "botAvatarUrl"
  | "guildName"
  | "guildAvatarUrl"
  | "senderFields"
  | "senderSelected"
  | "onSelectSender"
  | "onEditSender"
  | "presentation"
>;
