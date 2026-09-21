import { ArrowRight } from "lucide-react";
import { normalizeModuleId, type DashboardVisualModule } from "../moduleCatalog";
import { ModuleArtwork } from "./ModuleArtwork";

interface HomePageProps {
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function ModulesPage({ modules, onOpen }: HomePageProps) {
  const main = modules.filter((item) => item.group === "main");

  return <section className="osk-dashboard-page osk-home-page osk-modules-page">
    <header className="osk-home-intro"><h1>Módulos</h1></header>
    <div className="osk-function-grid">
      {main.map((item) => {
        const Icon = item.icon;
        const economy = normalizeModuleId(item.id) === "economy";
        return <button key={item.id} type="button" className="osk-function-card" data-module={item.id} data-wide={economy || undefined} onClick={() => onOpen(item.id)} aria-label={`Configurar ${item.label}`}>
          <ModuleArtwork moduleId={item.id} fallbackIcon={Icon} />
          <span className="osk-function-label">
            {!economy && <Icon size={17} aria-hidden="true" />}
            <strong>{item.label}</strong>
            <ArrowRight className="osk-function-arrow" size={16} aria-hidden="true" />
          </span>
        </button>;
      })}
    </div>
  </section>;
}

export const HomePage = ModulesPage;
