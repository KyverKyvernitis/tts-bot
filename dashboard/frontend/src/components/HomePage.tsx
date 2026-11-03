import { ArrowRight } from "lucide-react";
import { normalizeModuleId, type DashboardVisualModule } from "../moduleCatalog";
import { ModuleArtwork } from "./ModuleArtwork";

interface HomePageProps {
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function ModulesPage({ modules, onOpen }: HomePageProps) {
  const main = modules.filter((item) => item.group === "main");
  const featured = main.find((item) => normalizeModuleId(item.id) === "tts");
  const ordered = featured ? [featured, ...main.filter((item) => item !== featured)] : main;

  return <section className="osk-dashboard-page osk-home-page osk-modules-page">
    <header className="osk-workspace-page-heading"><h1>Módulos</h1><p>Escolha uma função para configurar.</p></header>
    <div className="osk-function-grid">
      {ordered.map((item) => {
        const moduleId = normalizeModuleId(item.id);
        return <button key={item.id} type="button" className="osk-function-card" data-module={item.id} data-wide={moduleId === "economy" || undefined} data-featured={moduleId === "tts" || undefined}
          disabled={!item.available} onClick={() => onOpen(item.id)} aria-label={`Configurar ${item.label}`}>
          <ModuleArtwork moduleId={item.id} fallbackIcon={item.icon} />
          <span className="osk-module-card-copy">
            <span className="osk-function-label"><strong>{item.label}</strong><ArrowRight className="osk-function-arrow" size={17} aria-hidden="true" /></span>
          </span>
        </button>;
      })}
    </div>
  </section>;
}

export const HomePage = ModulesPage;
