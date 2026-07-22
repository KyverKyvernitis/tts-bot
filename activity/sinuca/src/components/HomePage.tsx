import { ArrowRight, Info, LayoutGrid, Settings2, Sparkles } from "lucide-react";
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
      <span className="osk-home-intro-icon"><Sparkles size={22} /></span>
      <div>
        <span className="osk-kicker">{guildName}</span>
        <h1>Escolha o que quer ajustar.</h1>
        <p>Abra uma função, faça as mudanças e salve. Nada aqui é obrigatório.</p>
      </div>
    </header>

    <div className="osk-home-note">
      <Info size={17} />
      <span>Todas as funções são independentes. Configure somente o que fizer sentido para este servidor.</span>
    </div>

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
      {items.map((item) => {
        const Icon = item.icon;
        const hasExplicitState = item.enabled !== null;
        return <button key={item.id} className="osk-function-card" onClick={() => onOpen(item.id)}>
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
