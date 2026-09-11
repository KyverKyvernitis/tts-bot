import { MongoClient, type Collection, type Db, type Document } from "mongodb";
import { BIRTHDAY_DOC_CONFIG, WELCOME_DOC_CONFIG } from "../config/dashboardDocumentIds.js";
import { defaultBirthdayDoc, defaultGuildDoc, defaultWelcomeDoc } from "../config/dashboardDocumentDefaults.js";
import { applyLegacyFeatureFlags } from "../config/dashboardLegacyCompat.js";
import { deepMerge } from "../config/dashboardObjectUtils.js";
import { snowflakeToLong } from "../config/dashboardSnowflakes.js";
import { isDashboardTimeZone } from "../config/dashboardValidation.js";
import type { CreateDashboardConfigServiceOptions, DashboardFieldScope } from "../config/dashboardTypes.js";
import type { DashboardDocs } from "./dashboardConfigModel.js";

export interface DashboardConfigRepository {
  readAll(guildId: string): Promise<DashboardDocs>;
  readGuild(guildId: string): Promise<Record<string, unknown>>;
  saveDocs(guildId: string, patches: Map<DashboardFieldScope, Record<string, unknown>>, changedSections: string[]): Promise<number | undefined>;
}

export function createDashboardConfigRepository(options: CreateDashboardConfigServiceOptions): DashboardConfigRepository {
  let client: MongoClient | null = null;
  let db: Db | null = null;
  let coll: Collection<Document> | null = null;

  async function getCollection(): Promise<Collection<Document>> {
    if (!options.mongoUri) throw new Error("mongodb_not_configured");
    if (coll) return coll;
    client = new MongoClient(options.mongoUri);
    await client.connect();
    db = client.db(options.mongoDbName);
    coll = db.collection(options.mongoCollectionName);
    return coll;
  }

  async function readDoc(guildId: string, type: string, defaults: Record<string, unknown>) {
    const collection = await getCollection();
    const doc = await collection.findOne({ type, guild_id: snowflakeToLong(guildId) }, { projection: { _id: 0 } });
    const raw = (doc as Record<string, unknown> | null) ?? {};
    const merged = deepMerge(defaults, raw);
    applyLegacyFeatureFlags(type, raw, merged);
    return { raw, merged };
  }

  async function readAll(guildId: string): Promise<DashboardDocs> {
    const [guildDoc, welcomeDoc, birthdayDoc] = await Promise.all([
      readDoc(guildId, "guild", defaultGuildDoc(guildId)),
      readDoc(guildId, WELCOME_DOC_CONFIG, defaultWelcomeDoc(guildId)),
      readDoc(guildId, BIRTHDAY_DOC_CONFIG, defaultBirthdayDoc(guildId)),
    ]);
    const guild = guildDoc.merged;
    if (!isDashboardTimeZone(guildDoc.raw.timezone)) {
      guild.timezone = isDashboardTimeZone(birthdayDoc.raw.timezone)
        ? String(birthdayDoc.raw.timezone).trim()
        : "America/Sao_Paulo";
    }
    return { guild, welcome: welcomeDoc.merged, birthday: birthdayDoc.merged };
  }

  return {
    readAll,
    async readGuild(guildId: string) {
      return (await readDoc(guildId, "guild", defaultGuildDoc(guildId))).merged;
    },
    async saveDocs(guildId, patches, changedSections) {
      const collection = await getCollection();
      const guildIdValue = snowflakeToLong(guildId);
      const jobs: Promise<unknown>[] = [];
      for (const [scope, patch] of patches.entries()) {
        if (!Object.keys(patch).length) continue;
        const type = scope === "guild" ? "guild" : scope === "welcome" ? WELCOME_DOC_CONFIG : BIRTHDAY_DOC_CONFIG;
        jobs.push(collection.updateOne(
          { type, guild_id: guildIdValue },
          { $set: { type, guild_id: guildIdValue, ...patch } },
          { upsert: true },
        ));
      }
      await Promise.all(jobs);
      const revisionResult = await collection.findOneAndUpdate(
        { type: "guild", guild_id: guildIdValue },
        { $set: { type: "guild", guild_id: guildIdValue, dashboard_updated_at: Math.floor(Date.now() / 1000), dashboard_changed_sections: changedSections }, $inc: { dashboard_revision: 1 } },
        { upsert: true, returnDocument: "after", projection: { _id: 0, dashboard_revision: 1 } },
      );
      return typeof revisionResult?.dashboard_revision === "number" ? revisionResult.dashboard_revision : undefined;
    },
  };
}
