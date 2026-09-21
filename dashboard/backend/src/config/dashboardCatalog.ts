import type { DashboardFieldDefinition, DashboardSectionDefinition } from "./dashboardTypes.js";
import { birthdaySection } from "./sections/dashboardBirthdaySection.js";
import { colorRolesSection } from "./sections/dashboardColorRolesSection.js";
import { formsSection } from "./sections/dashboardFormsSection.js";
import { generalSection } from "./sections/dashboardGeneralSection.js";
import { ticketsSection } from "./sections/dashboardTicketsSection.js";
import { ttsSection } from "./sections/dashboardTtsSection.js";
import { welcomeSection } from "./sections/dashboardWelcomeSection.js";
import { economySection } from "./sections/dashboardEconomySection.js";

export const dashboardSections: DashboardSectionDefinition[] = [
  generalSection,
  welcomeSection,
  formsSection,
  ticketsSection,
  colorRolesSection,
  birthdaySection,
  ttsSection,
  economySection,
];

export function allDashboardFields(): DashboardFieldDefinition[] {
  return dashboardSections.flatMap((section) => section.fields);
}
