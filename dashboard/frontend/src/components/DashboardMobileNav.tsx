import { Command, LayoutGrid, Settings2 } from "lucide-react";
import type { DashboardNavigationPage } from "./Sidebar";

interface DashboardMobileNavProps {
  activePage: DashboardNavigationPage;
  onNavigate(page: DashboardNavigationPage): void;
}

const links = [
  { page: "general", label: "Geral", icon: Settings2 },
  { page: "modules", label: "Módulos", icon: LayoutGrid },
  { page: "commands", label: "Comandos", icon: Command },
] as const;

export function DashboardMobileNav({ activePage, onNavigate }: DashboardMobileNavProps) {
  return <nav className="osk-mobile-navigation" aria-label="Navegação principal">
    {links.map(({ page, label, icon: Icon }) => <button key={page} type="button" aria-current={activePage === page ? "page" : undefined} onClick={() => onNavigate(page)}><Icon size={19} aria-hidden="true" /><span>{label}</span></button>)}
  </nav>;
}
