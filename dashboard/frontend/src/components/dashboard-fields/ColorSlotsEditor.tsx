import type { DashboardColorSlot, DashboardFieldDefinition, DashboardRoleOption } from "../../types/dashboard";
import { SmartSelect } from "../SmartSelect";
import { colorRoleHex, colorRoleLabel } from "../color-roles/colorRolesModel";

export function ColorSlotsEditor({
  field,
  value,
  roles,
  selectedSlot,
  visibleSlotIds,
  onSelectSlot,
  onChange,
}: {
  field: DashboardFieldDefinition;
  value: unknown;
  roles: DashboardRoleOption[];
  selectedSlot?: number | null;
  visibleSlotIds?: number[] | null;
  onSelectSlot?(slotNumber: number): void;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}) {
  const slots = value && typeof value === "object" ? value as Record<string, DashboardColorSlot> : {};
  const order = visibleSlotIds?.length ? visibleSlotIds : Object.keys(slots).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
  const visible = selectedSlot && order.includes(selectedSlot) ? [selectedSlot] : order;
  const availableRoles = roles
    .filter((role) => !role.managed && role.assignable !== false)
    .map((role) => {
      const roleColor = Math.max(0, Number(role.color || 0));
      return {
        value: role.id,
        label: `@${role.name}`,
        hint: roleColor > 0
          ? `Cor do cargo: #${roleColor.toString(16).padStart(6, "0").slice(-6).toUpperCase()}`
          : "Cargo sem cor própria",
      };
    });

  const update = (slotNumber: number, patch: Partial<DashboardColorSlot>) => {
    const key = String(slotNumber);
    const current = slots[key] || ({ number: slotNumber, name: `Cor ${slotNumber}`, text_hex: "#ffffff", role_hex: "#ffffff", role_id: 0, role_name: `Cor ${slotNumber}`, managed: false } as DashboardColorSlot);
    onChange(field, { ...slots, [key]: { ...current, ...patch, number: slotNumber } });
  };

  if (!visible.length) return <div className="osk-inline-note">Nenhuma opção está disponível neste painel.</div>;

  return <div className="osk-color-slot-inspector">
    {visible.map((slotNumber) => {
      const key = String(slotNumber);
      const slot = slots[key] || ({ number: slotNumber, name: `Cor ${slotNumber}`, text_hex: "#ffffff", role_hex: "#ffffff", role_id: 0, role_name: `Cor ${slotNumber}`, managed: false } as DashboardColorSlot);
      const currentRoleId = String(slot.role_id || "");
      const currentRole = roles.find((role) => role.id === currentRoleId);
      const selectOptions = currentRoleId && !availableRoles.some((role) => role.value === currentRoleId)
        ? [{ value: currentRoleId, label: `@${currentRole?.name || slot.role_name || currentRoleId}`, hint: "Cargo atual indisponível para atribuição" }, ...availableRoles]
        : availableRoles;
      const color = colorRoleHex(slot, { ok: true, channels: [], roles });
      const roleHasOwnColor = Boolean(currentRole && Number(currentRole.color || 0) > 0);
      const colorDescription = currentRole
        ? (roleHasOwnColor ? `${color} · atualizada automaticamente pelo Discord` : "Cargo sem cor própria · prévia neutra")
        : `${color} · cor do preset usada enquanto não há cargo válido`;
      const visibleIndex = visibleSlotIds?.indexOf(slotNumber) ?? -1;
      return <article key={key} data-slot-number={slotNumber}>
        <header>
          <span className="osk-color-slot-swatch" style={{ background: color }}><b>{visibleIndex >= 0 ? visibleIndex + 1 : slotNumber}</b></span>
          <div><strong>{String(slot.name || `Cor ${slotNumber}`)}</strong><small>{colorRoleLabel(slot, { ok: true, channels: [], roles })}</small></div>
        </header>
        <label><span>Nome exibido</span><input value={String(slot.name || "")} maxLength={80} onFocus={() => onSelectSlot?.(slotNumber)} onChange={(event) => update(slotNumber, { name: event.target.value })} /></label>
        <label><span>Cargo vinculado</span>{availableRoles.length || currentRoleId ? <SmartSelect id={`color-slot-${key}`} ariaLabel={`Cargo vinculado à opção ${visibleIndex >= 0 ? visibleIndex + 1 : slotNumber}`} value={currentRoleId} options={[{ value: "", label: "Nenhum cargo" }, ...selectOptions]} onChange={(next) => {
          const selected = roles.find((role) => role.id === next);
          update(slotNumber, { role_id: next, role_name: selected?.name || String(slot.name || `Cor ${slotNumber}`), managed: false });
        }} placeholder="Selecione o cargo" /> : <div className="osk-inline-note">Nenhum cargo atribuível foi encontrado.</div>}</label>
        <div className="osk-color-slot-derived"><span className="osk-color-slot-derived__dot" style={{ background: color }} /><div><strong>Cor do cargo</strong><small>{colorDescription}</small></div></div>
      </article>;
    })}
  </div>;
}
