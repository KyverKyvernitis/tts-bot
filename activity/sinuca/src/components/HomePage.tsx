import { ArrowRight, LayoutGrid, Settings2 } from "lucide-react";
import type { CSSProperties } from "react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  guildName: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function HomePage({ guildName, modules, onOpen }: HomePageProps) {
  const main = modules.filter((item) => item.group === "main");
  const system = modules.filter((item) => item.group === "system");

  return <section className="osk-dashboard-page osk-home-page">
    <header className="osk-home-intro">
      <span className="osk-kicker">{guildName}</span>
      <h1>Configure a Osaka do seu jeito.</h1>
      <p>Escolha uma função abaixo. Todas são opcionais e independentes.</p>
    </header>

    <FunctionGroup title="Funções do servidor" icon={LayoutGrid} items={main} onOpen={onOpen} />
    <FunctionGroup title="Configurações do bot" icon={Settings2} items={system} onOpen={onOpen} />
  </section>;
}

function FunctionGroup({
  title,
  icon: GroupIcon,
  items,
  onOpen,
}: {
  title: string;
  icon: typeof LayoutGrid;
  items: DashboardVisualModule[];
  onOpen(id: string): void;
}) {
  if (!items.length) return null;

  return <section className="osk-function-group">
    <header><GroupIcon size={16} /><h2>{title}</h2></header>
    <div className="osk-function-grid">
      {items.map((item, index) => {
        const Icon = item.icon;
        const hasExplicitState = item.enabled !== null;
        return <button key={item.id} className="osk-function-card" style={{ "--osk-card-index": index } as CSSProperties} onClick={() => onOpen(item.id)}>
          <span className="osk-function-icon"><Icon size={21} /></span>
          <span className="osk-function-copy">
            <strong>{item.label}</strong>
            <small>{item.description}</small>
          </span>
          {hasExplicitState && <span className="osk-function-state" data-enabled={item.enabled || undefined}>{item.enabled ? "Ativada" : "Desativada"}</span>}
          <span className="osk-function-arrow"><ArrowRight size={18} /></span>
        </button>;
      })}
    </div>
  </section>;
}
