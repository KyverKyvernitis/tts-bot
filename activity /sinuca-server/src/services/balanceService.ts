import { Long, MongoClient } from "mongodb";
import type {
  BalanceDebugSnapshot,
  BalanceSnapshot,
  SessionContextPayload,
} from "../messages.js";
import { normalizeIntString } from "../shared/session.js";

export interface BalanceServiceConfig {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName: string;
}

export interface BalanceLookupResult {
  balance: BalanceSnapshot;
  debug: BalanceDebugSnapshot;
}

export interface UserBalanceState extends BalanceSnapshot {
  docFound: boolean;
}

export interface MatchStakeSettlementInput {
  guildId: string;
  winnerUserId: string;
  loserUserId: string;
  stakeChips: number;
}

export type StakeSpendReason = "ok" | "bonus_confirm_required" | "debt_confirm_required" | "negative_confirm_required" | "insufficient_chips";

export interface StakeSpendPreview {
  currentChips: number;
  currentBonusChips: number;
  resultingChips: number;
  resultingBonusChips: number;
  bonusToUse: number;
  normalToUse: number;
  canProceed: boolean;
  needsConfirmation: boolean;
  reason: StakeSpendReason;
}

export interface BalanceService {
  readonly config: BalanceServiceConfig;
  fetchBalance(guildId: string, userId: string, session?: SessionContextPayload): Promise<BalanceLookupResult>;
  getUserBalanceState(guildId: string, userId: string): Promise<UserBalanceState>;
  previewStakeSpend(guildId: string, userId: string, stakeChips: number): Promise<StakeSpendPreview>;
  applyMatchStakeSettlement(input: MatchStakeSettlementInput): Promise<void>;
  buildMissingIdentifiersDebug(args: {
    session?: SessionContextPayload | null;
    guildId: string | null;
    userId: string | null;
    note: string;
  }): BalanceDebugSnapshot;
  buildErrorDebug(args: {
    session?: SessionContextPayload | null;
    guildId: string;
    userId: string;
    note: string;
  }): BalanceDebugSnapshot;
}

export function createBalanceService(config: BalanceServiceConfig): BalanceService {
  let mongoClient: MongoClient | null = null;
  const DEFAULT_NORMAL_CHIPS = 100;
  const MAX_CHIP_DEBT = 100;

  async function ensureMongo() {
    if (!config.mongoUri) return null;
    if (!mongoClient) {
      mongoClient = new MongoClient(config.mongoUri);
      await mongoClient.connect();
    }
    return mongoClient.db(config.mongoDbName).collection(config.mongoCollectionName);
  }

  function toMongoLong(value: string | null | undefined): Long | null {
    const normalized = normalizeIntString(value);
    if (!normalized || !/^\d+$/.test(normalized)) return null;
    try {
      return Long.fromString(normalized, true);
    } catch {
      return null;
    }
  }

  function buildBalanceQuery(guildId: string, userId: string): { mongo: { type: string; guild_id: Long | string; user_id: Long | string }; debug: Record<string, string | null> } {
    const normalizedGuildId = normalizeIntString(guildId);
    const normalizedUserId = normalizeIntString(userId);
    const guildLong = toMongoLong(normalizedGuildId);
    const userLong = toMongoLong(normalizedUserId);

    return {
      mongo: {
        type: "user",
        guild_id: guildLong ?? normalizedGuildId ?? "",
        user_id: userLong ?? normalizedUserId ?? "",
      },
      debug: {
        type: "user",
        guild_id: normalizedGuildId,
        user_id: normalizedUserId,
      },
    };
  }

  function buildMissingIdentifiersDebug(args: {
    session?: SessionContextPayload | null;
    guildId: string | null;
    userId: string | null;
    note: string;
  }): BalanceDebugSnapshot {
    return {
      source: "missing_identifiers",
      sessionUserId: args.session?.userId ?? null,
      sessionGuildId: args.session?.guildId ?? null,
      requestUserId: args.userId ?? null,
      requestGuildId: args.guildId ?? null,
      mongoConnected: Boolean(config.mongoUri),
      mongoDbName: config.mongoDbName,
      mongoCollectionName: config.mongoCollectionName,
      query: { type: "user", guild_id: args.guildId ?? null, user_id: args.userId ?? null },
      docFound: false,
      docKeys: [],
      rawChips: null,
      rawBonusChips: null,
      normalizedChips: 0,
      normalizedBonusChips: 0,
      note: args.note,
    };
  }

  function buildErrorDebug(args: {
    session?: SessionContextPayload | null;
    guildId: string;
    userId: string;
    note: string;
  }): BalanceDebugSnapshot {
    return {
      source: "balance_error",
      sessionUserId: args.session?.userId ?? null,
      sessionGuildId: args.session?.guildId ?? null,
      requestUserId: args.userId,
      requestGuildId: args.guildId,
      mongoConnected: Boolean(config.mongoUri),
      mongoDbName: config.mongoDbName,
      mongoCollectionName: config.mongoCollectionName,
      query: { type: "user", guild_id: args.guildId, user_id: args.userId },
      docFound: false,
      docKeys: [],
      rawChips: null,
      rawBonusChips: null,
      normalizedChips: 0,
      normalizedBonusChips: 0,
      note: args.note,
    };
  }



  async function getBalanceCollection() {
    return await ensureMongo();
  }

  async function findUserDoc(guildId: string, userId: string) {
    const coll = await getBalanceCollection();
    if (!coll) return { coll: null, doc: null };
    const querySpec = buildBalanceQuery(guildId, userId);
    let doc = await coll.findOne(querySpec.mongo);
    if (!doc) {
      const stringQuery = { type: "user", guild_id: querySpec.debug.guild_id ?? "", user_id: querySpec.debug.user_id ?? "" };
      if (stringQuery.guild_id && stringQuery.user_id) {
        doc = await coll.findOne(stringQuery);
      }
    }
    return { coll, doc };
  }

  async function getUserBalanceState(guildId: string, userId: string): Promise<UserBalanceState> {
    const { doc } = await findUserDoc(guildId, userId);
    if (!doc) {
      return { chips: DEFAULT_NORMAL_CHIPS, bonusChips: 0, docFound: false };
    }
    const chips = Number(doc?.chips ?? DEFAULT_NORMAL_CHIPS);
    const bonusChips = Number(doc?.bonus_chips ?? 0);
    return {
      chips: Number.isFinite(chips) ? chips : DEFAULT_NORMAL_CHIPS,
      bonusChips: Number.isFinite(bonusChips) ? bonusChips : 0,
      docFound: true,
    };
  }

  async function previewStakeSpend(guildId: string, userId: string, stakeChips: number): Promise<StakeSpendPreview> {
    const state = await getUserBalanceState(guildId, userId);
    const spend = Math.max(0, Number(stakeChips ?? 0) || 0);
    const currentChips = Number(state.chips ?? DEFAULT_NORMAL_CHIPS);
    const currentBonusChips = Math.max(0, Number(state.bonusChips ?? 0) || 0);
    const bonusToUse = Math.min(currentBonusChips, spend);
    const normalToUse = spend - bonusToUse;
    const resultingBonusChips = currentBonusChips - bonusToUse;
    const resultingChips = currentChips - normalToUse;
    if (resultingChips < -MAX_CHIP_DEBT) {
      return {
        currentChips,
        currentBonusChips,
        resultingChips,
        resultingBonusChips,
        bonusToUse,
        normalToUse,
        canProceed: false,
        needsConfirmation: false,
        reason: "insufficient_chips",
      };
    }
    if (resultingChips < 0) {
      return {
        currentChips,
        currentBonusChips,
        resultingChips,
        resultingBonusChips,
        bonusToUse,
        normalToUse,
        canProceed: true,
        needsConfirmation: true,
        reason: currentChips < 0 ? "negative_confirm_required" : "debt_confirm_required",
      };
    }
    if (bonusToUse > 0) {
      return {
        currentChips,
        currentBonusChips,
        resultingChips,
        resultingBonusChips,
        bonusToUse,
        normalToUse,
        canProceed: true,
        needsConfirmation: true,
        reason: "bonus_confirm_required",
      };
    }
    return {
      currentChips,
      currentBonusChips,
      resultingChips,
      resultingBonusChips,
      bonusToUse,
      normalToUse,
      canProceed: true,
      needsConfirmation: false,
      reason: "ok",
    };
  }

  async function applyChipDelta(guildId: string, userId: string, delta: number) {
    const { coll, doc } = await findUserDoc(guildId, userId);
    if (!coll) throw new Error('mongo_unavailable');
    const normalizedGuildId = normalizeIntString(guildId) ?? guildId;
    const normalizedUserId = normalizeIntString(userId) ?? userId;
    const guildLong = toMongoLong(normalizedGuildId);
    const userLong = toMongoLong(normalizedUserId);
    if (doc?._id) {
      await coll.updateOne({ _id: doc._id }, { $inc: { chips: delta } });
      return;
    }
    await coll.insertOne({
      type: 'user',
      guild_id: guildLong ?? normalizedGuildId,
      user_id: userLong ?? normalizedUserId,
      chips: DEFAULT_NORMAL_CHIPS + delta,
      bonus_chips: 0,
    });
  }

  async function applyStakeCost(guildId: string, userId: string, stakeChips: number): Promise<StakeSpendPreview> {
    const preview = await previewStakeSpend(guildId, userId, stakeChips);
    if (!preview.canProceed) {
      throw new Error('insufficient_chips');
    }
    const { coll, doc } = await findUserDoc(guildId, userId);
    if (!coll) throw new Error('mongo_unavailable');
    const normalizedGuildId = normalizeIntString(guildId) ?? guildId;
    const normalizedUserId = normalizeIntString(userId) ?? userId;
    const guildLong = toMongoLong(normalizedGuildId);
    const userLong = toMongoLong(normalizedUserId);
    if (doc?._id) {
      await coll.updateOne({ _id: doc._id }, { $inc: { chips: -preview.normalToUse, bonus_chips: -preview.bonusToUse } });
      return preview;
    }
    await coll.insertOne({
      type: 'user',
      guild_id: guildLong ?? normalizedGuildId,
      user_id: userLong ?? normalizedUserId,
      chips: preview.resultingChips,
      bonus_chips: preview.resultingBonusChips,
    });
    return preview;
  }

  async function applyMatchStakeSettlement(input: MatchStakeSettlementInput): Promise<void> {
    const stake = Math.max(0, Number(input.stakeChips ?? 0) || 0);
    if (!stake) return;
    await applyChipDelta(input.guildId, input.winnerUserId, stake);
    await applyStakeCost(input.guildId, input.loserUserId, stake);
  }

  async function fetchBalance(guildId: string, userId: string, session?: SessionContextPayload): Promise<BalanceLookupResult> {
    const coll = await ensureMongo();
    const querySpec = buildBalanceQuery(guildId, userId);
    if (!coll) {
      return {
        balance: { chips: 0, bonusChips: 0 },
        debug: {
          source: "fallback_no_mongo",
          sessionUserId: session?.userId ?? null,
          sessionGuildId: session?.guildId ?? null,
          requestUserId: userId,
          requestGuildId: guildId,
          mongoConnected: false,
          mongoDbName: config.mongoDbName,
          mongoCollectionName: config.mongoCollectionName,
          query: querySpec.debug,
          docFound: false,
          docKeys: [],
          rawChips: null,
          rawBonusChips: null,
          normalizedChips: 0,
          normalizedBonusChips: 0,
          note: "mongo indisponível; usando fallback 0/0",
        },
      };
    }

    let doc = await coll.findOne(querySpec.mongo, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
    let querySource = querySpec.mongo.guild_id instanceof Long || querySpec.mongo.user_id instanceof Long ? "mongo_long" : "mongo_string";

    if (!doc) {
      const stringQuery = { type: "user", guild_id: querySpec.debug.guild_id ?? "", user_id: querySpec.debug.user_id ?? "" };
      if (stringQuery.guild_id && stringQuery.user_id) {
        doc = await coll.findOne(stringQuery, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
        if (doc) querySource = "mongo_string";
      }
    }

    const chips = Number(doc?.chips ?? 0);
    const bonusChips = Number(doc?.bonus_chips ?? 0);
    const balance = {
      chips: Number.isFinite(chips) ? chips : 0,
      bonusChips: Number.isFinite(bonusChips) ? bonusChips : 0,
    };
    const debug: BalanceDebugSnapshot = {
      source: doc ? querySource : "mongo_default",
      sessionUserId: session?.userId ?? null,
      sessionGuildId: session?.guildId ?? null,
      requestUserId: userId,
      requestGuildId: guildId,
      mongoConnected: true,
      mongoDbName: config.mongoDbName,
      mongoCollectionName: config.mongoCollectionName,
      query: querySpec.debug,
      docFound: Boolean(doc),
      docKeys: doc ? Object.keys(doc).sort() : [],
      rawChips: doc?.chips ?? null,
      rawBonusChips: doc?.bonus_chips ?? null,
      normalizedChips: balance.chips,
      normalizedBonusChips: balance.bonusChips,
      note: doc ? "consulta executada" : "documento do usuário não encontrado com essa guild/user",
    };
    console.log("[sinuca-balance]", JSON.stringify(debug));
    return { balance, debug };
  }

  return {
    config,
    fetchBalance,
    getUserBalanceState,
    previewStakeSpend,
    applyMatchStakeSettlement,
    buildMissingIdentifiersDebug,
    buildErrorDebug,
  };
}
