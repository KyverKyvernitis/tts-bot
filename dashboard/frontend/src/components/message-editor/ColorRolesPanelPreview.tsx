import { ArrowDown, ArrowUp, Minus, Plus } from "lucide-react";
import type { CSSProperties } from "react";
import type { DashboardColorSlot } from "../../types/dashboard";
import {
  COLOR_PANEL_OPTION_MAX,
  colorPanelFromEditorId,
  colorRoleHex,
  nextUnusedColorSlot,
  normalizeColorPanelLayout,
  updatePanelSlots,
} from "../color-roles/colorRolesModel";
import { needsLightOutline } from "./messagePreviewModel";
import type { PreviewCoreProps } from "./messagePreviewTypes";

export function ColorRolesPanelPreview(props: PreviewCoreProps) {
  const { editorId = "", fields, draft, guildOptions, interactive, selectedFieldId, selectedColorSlot, onSelectField, onChange, onSelectColorSlot } = props;
  const layoutField = fields.find((field) => field.id === "color_roles.panel_layout");
  const slotsField = fields.find((field) => field.id === "color_roles.slots");
  const layout = normalizeColorPanelLayout(draft["color_roles.panel_layout"]);
  const panel = colorPanelFromEditorId(layout, editorId) ?? layout[0];
  const panelIndex = Math.max(0, layout.findIndex((item) => item.id === panel?.id));
  const rawSlots = draft["color_roles.slots"];
  const slots = rawSlots && typeof rawSlots === "object" ? rawSlots as Record<string, DashboardColorSlot> : {};
  const panelSlots = (panel?.slots ?? []).map((number) => ({
    ...(slots[String(number)] || ({ number, name: `Cor ${number}`, text_hex: "", role_hex: "", role_id: 0, role_name: `Cor ${number}`, managed: false } as DashboardColorSlot)),
    number,
  }));

  const commitLayout = (next: typeof layout) => {
    if (layoutField && onChange) onChange(layoutField, next);
  };
  const moveOption = (index: number, direction: -1 | 1) => {
    if (!panel) return;
    const target = index + direction;
    if (target < 0 || target >= panel.slots.length) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => {
      [current[index], current[target]] = [current[target], current[index]];
      return current;
    }));
  };
  const removeOption = (slotNumber: number) => {
    if (!panel || panel.slots.length <= 1) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => current.filter((number) => number !== slotNumber)));
    if (selectedColorSlot === slotNumber) onSelectColorSlot?.(panel.slots.find((number) => number !== slotNumber) ?? panel.slots[0]);
  };
  const addOption = () => {
    if (!panel || panel.slots.length >= COLOR_PANEL_OPTION_MAX) return;
    const number = nextUnusedColorSlot(layout);
    if (number === null) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => [...current, number]));
    if (slotsField) onSelectField?.(slotsField);
    onSelectColorSlot?.(number, true);
  };

  const selectedIndex = selectedColorSlot == null
    ? -1
    : panelSlots.findIndex((slot) => slot.number === selectedColorSlot);
  const selectedSlot = selectedIndex >= 0 ? panelSlots[selectedIndex] : null;
  const selectSlot = (slotNumber: number, openInspector = false) => {
    if (!interactive || !slotsField) return;
    onSelectField?.(slotsField);
    onSelectColorSlot?.(slotNumber, openInspector);
  };

  return (
    <div className="osk-color-panel-canvas" data-panel={panelIndex + 1}>
      <div className="osk-color-panel-canvas__image" aria-label={`Imagem de opções do Painel ${panelIndex + 1}`}>
        {panelSlots.map((slot, index) => {
          const color = colorRoleHex(slot, guildOptions);
          const selected = selectedColorSlot === slot.number;
          return (
            <button
              type="button"
              key={slot.number}
              className="osk-color-panel-canvas__slot"
              data-selected={selected || undefined}
              data-message-field-anchor={selected ? "color_roles.slots" : undefined}
              data-dark-color={needsLightOutline(color) || undefined}
              style={{
                "--osk-slot-color": color,
                color,
                WebkitTextFillColor: color,
              } as CSSProperties}
              disabled={!interactive || !slotsField}
              onClick={(event) => { event.stopPropagation(); selectSlot(slot.number); }}
              onDoubleClick={(event) => { event.stopPropagation(); selectSlot(slot.number, true); }}
            >
              <b>{index + 1}.</b><span>{String(slot.name || `Cor ${slot.number}`)}</span>
            </button>
          );
        })}
      </div>

      {interactive && selectedSlot && (
        <div className="osk-color-panel-canvas__selection-tools">
          <span><strong>Opção {selectedIndex + 1}</strong><small>{String(selectedSlot.name || `Cor ${selectedSlot.number}`)}</small></span>
          <span>
            <button type="button" disabled={selectedIndex <= 0} onClick={() => moveOption(selectedIndex, -1)} aria-label="Mover opção para cima"><ArrowUp size={14} /></button>
            <button type="button" disabled={selectedIndex < 0 || selectedIndex >= panelSlots.length - 1} onClick={() => moveOption(selectedIndex, 1)} aria-label="Mover opção para baixo"><ArrowDown size={14} /></button>
            <button type="button" disabled={panelSlots.length <= 1} onClick={() => removeOption(selectedSlot.number)} aria-label="Remover opção"><Minus size={15} /></button>
          </span>
        </div>
      )}

      {interactive && slotsField && panel && panel.slots.length < COLOR_PANEL_OPTION_MAX && nextUnusedColorSlot(layout) !== null && (
        <button type="button" className="osk-color-panel-canvas__add" onClick={(event) => { event.stopPropagation(); addOption(); }}><Plus size={15} />Adicionar opção</button>
      )}
    </div>
  );
}
