import { Clock3 } from "lucide-react";
import { useState } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { birthdayTime, parseBirthdayTime } from "./moduleAreas";

interface Props { fields: DashboardFieldDefinition[]; draft: Record<string, unknown>; onChange(field: DashboardFieldDefinition, value: unknown): void }
export function BirthdayTimeField({ fields, draft, onChange }: Props) {
  const hour = fields.find(field => field.id === "birthday.announce_hour");
  const minute = fields.find(field => field.id === "birthday.announce_minute");
  const [empty, setEmpty] = useState(false);
  if (!hour || !minute) return null;
  return <div className="osk-birthday-time"><Clock3 size={21} aria-hidden="true" /><div><strong>Horário dos avisos</strong><small>{String(draft["general.timezone"] || "America/Sao_Paulo").replace(/_/g, " ")} · fuso definido em Geral</small></div>
    <input type="time" aria-label="Horário dos avisos" required value={empty ? "" : birthdayTime(draft)} onBlur={() => setEmpty(false)} onChange={event => {
      const time = parseBirthdayTime(event.target.value);
      setEmpty(!time);
      if (time) { onChange(hour, time[0]); onChange(minute, time[1]); }
    }} />
  </div>;
}
