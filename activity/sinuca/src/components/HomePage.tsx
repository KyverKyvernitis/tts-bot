import { ArrowUpRight, CheckCircle2, CircleDashed, LayoutGrid, SlidersHorizontal } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  guildName: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function HomePage({ guildName, modules, onOpen }: HomePageProps) {
  const configured = modules.reduce((total, module) => total + module.configured, 0);
  const total = modules.reduce((totalValue, module) => totalValue + module.total, 0);
  const configuredModules = modules.filter((module) => module.enabled !== false && module.configured > 0).length;
  const percent = total > 0 ? Math.round((configured / total) * 100) : 0;
  const main = modules.filter((module) => module.group === "main");
  const system = modules.filter((module) => module.group === "system");

  return <section className="osk-dashboard-page">
    <div className="osk-dashboard-hero">
      <div>
        <span className="osk-kicker">Visão geral · {guildName}</span>
        <h1>Configuração clara, sem comandos administrativos.</h1>
        <p>Abra um módulo, revise os valores atuais e salve somente o que mudou.</p>
      </div>
      <div className="osk-dashboard-progress" style={{ "--progress": `${percent * 3.6}deg` } as React.CSSProperties}>
        <span><strong>{percent}%</strong><small>configurado</small></span>
      </div>
    </div>

    <div className="osk-dashboard-stats">
      <article><span><SlidersHorizontal size={18} /></span><div><strong>{modules.length}</strong><small>módulos disponíveis</small></div></article>
      <article><span><CheckCircle2 size={18} /></span><div><strong>{configuredModules}</strong><small>módulos configurados</small></div></article>
      <article><span><CircleDashed size={18} /></span><div><strong>{configured}/{total}</strong><small>campos configurados</small></div></article>
    </div>

    <ModuleGroup title="Módulos do servidor" modules={main} onOpen={onOpen} />
    <ModuleGroup title="Configurações gerais" modules={system} onOpen={onOpen} />
  </section>;
}

function ModuleGroup({ title, modules, onOpen }: { title: string; modules: DashboardVisualModule[]; onOpen(id: string): void }) {
  if (!modules.length) return null;
  return <section className="osk-dashboard-module-group">
    <header><span><LayoutGrid size={15} /></span><h2>{title}</h2><i /></header>
    <div className="osk-dashboard-module-grid">
      {modules.map((module) => {
        const Icon = module.icon;
        const percent = module.total > 0 ? Math.round((module.configured / module.total) * 100) : 0;
        const state = module.enabled === false ? "off" : module.configured > 0 ? "ready" : "partial";
        return <button key={module.id} className="osk-dashboard-module-card" data-state={state} onClick={() => onOpen(module.id)}>
          <span className="osk-dashboard-module-icon"><Icon size={21} /></span>
          <span className="osk-dashboard-module-copy"><strong>{module.label}</strong><small>{module.description}</small></span>
          <span className="osk-dashboard-module-meta"><em>{module.enabled === false ? "Desativado" : module.status}</em><i><b style={{ width: `${percent}%` }} /></i></span>
          <ArrowUpRight size={18} />
        </button>;
      })}
    </div>
  </section>;
}
