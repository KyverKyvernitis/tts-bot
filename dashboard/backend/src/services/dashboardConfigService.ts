import { dashboardSections } from "../config/dashboardCatalog.js";
import { clone } from "../config/dashboardObjectUtils.js";
import type { CreateDashboardConfigServiceOptions, DashboardConfigService } from "../config/dashboardTypes.js";
import {
  dashboardCommandContextFromGuild,
  dashboardSummaryFromDocs,
  dashboardValuesFromDocs,
  planDashboardUpdates,
} from "./dashboardConfigModel.js";
import { createDashboardConfigRepository } from "./dashboardConfigRepository.js";
import { loadDashboardEdgeVoices } from "./dashboardEdgeVoiceCatalog.js";

export * from "../config/dashboardTypes.js";
export { resolveDashboardSectionState } from "../config/dashboardSectionState.js";
export { applyLegacyFeatureFlags } from "../config/dashboardLegacyCompat.js";

export function createDashboardConfigService(options: CreateDashboardConfigServiceOptions): DashboardConfigService {
  const repository = createDashboardConfigRepository(options);
  return {
    listSections() { return clone(dashboardSections); },
    async getSummary(guildId: string) {
      return dashboardSummaryFromDocs(guildId, await repository.readAll(guildId));
    },
    async getSettings(guildId: string) {
      const docs = await repository.readAll(guildId);
      return { guildId, sections: clone(dashboardSections), values: dashboardValuesFromDocs(docs) };
    },
    async getCommandContext(guildId: string) {
      return dashboardCommandContextFromGuild(await repository.readGuild(guildId));
    },
    async updateSettings(guildId: string, updates: Record<string, unknown>) {
      const docs = await repository.readAll(guildId);
      const catalog = Object.prototype.hasOwnProperty.call(updates, "tts.voice") ? await loadDashboardEdgeVoices() : null;
      const plan = planDashboardUpdates(docs, updates, catalog?.voices.map(voice => voice.value));
      const revision = plan.saved.length
        ? await repository.saveDocs(guildId, plan.patches, plan.changedSections)
        : undefined;
      return {
        ok: true as const,
        values: plan.values,
        saved: plan.saved,
        revision,
        changed_sections: plan.changedSections,
      };
    },
  };
}
