import type { Express, Request, Response } from "express";
import { RequestHandler } from "express";
import {
  addPlayer,
  createRoom,
  getRoom,
  removePlayer,
  setPlayerReady,
  setPlayerStakeGateAcceptance,
  setRoomInGame,
  setRoomStake,
  toSnapshot,
  listRooms,
  toggleRematchReady,
  clearRematchReady,
  type PlayerRef,
} from "../rooms.js";
import {
  getInitialRuleSet,
} from "../gameRules.js";
import {
  getGameSnapshot,
  removeGame,
  startGameForRoom,
  takeShotChecked,
} from "../gameState.js";
import type {
  BalanceDebugSnapshot,
  ListRoomsPayload,
  SessionContextPayload,
} from "../messages.js";
import {
  BALANCE_ACTIONS,
  BALANCE_ROUTE_PATHS,
  GAME_ROUTE_PATHS,
  HEALTH_ROUTE_PATHS,
  ROOM_CLOSE_REASONS,
  ROOM_ROUTE_PATHS,
  SESSION_ROUTE_PATHS,
  TOKEN_ROUTE_PATHS,
} from "../shared/contracts.js";
import { registerGetOnly, registerGetPost, registerPostOnly, sendNoStoreJson } from "../shared/http.js";
import {
  booleanish,
  firstString,
  mergeWithSession,
  normalizeIntString,
  normalizeRoomMode,
  resolveRequestSession,
} from "../shared/session.js";
import type { BalanceService } from "../services/balanceService.js";
import type { ActivityRealtimeRuntime } from "../realtime/runtime.js";

export interface RegisterActivityRoutesOptions {
  app: Express;
  runtime: ActivityRealtimeRuntime;
  balanceService: BalanceService;
  exchangeDiscordCode(code: string): Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }>;
}

export function registerActivityRoutes({ app, runtime, balanceService, exchangeDiscordCode }: RegisterActivityRoutesOptions) {

  function previewReasonToAcceptanceKind(reason: Awaited<ReturnType<typeof balanceService.previewStakeSpend>>["reason"]) {
    if (reason === "bonus_confirm_required") return "bonus" as const;
    if (reason === "debt_confirm_required") return "debt" as const;
    if (reason === "negative_confirm_required") return "negative" as const;
    return "ok" as const;
  }

  function applyStakeGateAcceptance(roomId: string, userId: string, requiredChips: number, preview: Awaited<ReturnType<typeof balanceService.previewStakeSpend>>) {
    setPlayerStakeGateAcceptance(roomId, userId, {
      kind: previewReasonToAcceptanceKind(preview.reason),
      stakeChips: requiredChips,
      resultingChips: preview.resultingChips,
      resultingBonusChips: preview.resultingBonusChips,
      bonusToUse: preview.bonusToUse,
      acceptedAt: Date.now(),
    });
  }

  function acceptanceMatchesPreview(player: PlayerRef | undefined, requiredChips: number, preview: Awaited<ReturnType<typeof balanceService.previewStakeSpend>>) {
    const acceptance = player?.stakeGateAcceptance;
    if (!acceptance) return false;
    return acceptance.kind === previewReasonToAcceptanceKind(preview.reason)
      && acceptance.stakeChips === requiredChips
      && acceptance.resultingChips === preview.resultingChips
      && acceptance.resultingBonusChips === preview.resultingBonusChips
      && acceptance.bonusToUse === preview.bonusToUse;
  }


  function buildStakeGateResponse(args: {
    error: string;
    detail: string;
    blockedUserId: string;
    requiredChips: number;
    currentChips: number;
    currentBonusChips: number;
    resultingChips: number;
    resultingBonusChips: number;
    bonusToUse: number;
  }) {
    return {
      error: args.error,
      detail: args.detail,
      blockedUserId: args.blockedUserId,
      requiredChips: args.requiredChips,
      currentChips: args.currentChips,
      currentBonusChips: args.currentBonusChips,
      resultingChips: args.resultingChips,
      resultingBonusChips: args.resultingBonusChips,
      bonusToUse: args.bonusToUse,
    } as const;
  }

  async function buildStakePreviewResponse(args: { guildId: string; userId: string; requiredChips: number }) {
    const preview = await balanceService.previewStakeSpend(args.guildId, args.userId, args.requiredChips);
    if (preview.reason === "bonus_confirm_required") {
      return buildStakeGateResponse({
        error: "bonus_confirm_required",
        detail: "Usar fichas bônus?",
        blockedUserId: args.userId,
        requiredChips: args.requiredChips,
        currentChips: preview.currentChips,
        currentBonusChips: preview.currentBonusChips,
        resultingChips: preview.resultingChips,
        resultingBonusChips: preview.resultingBonusChips,
        bonusToUse: preview.bonusToUse,
      });
    }
    if (preview.reason === "debt_confirm_required") {
      return buildStakeGateResponse({
        error: "debt_confirm_required",
        detail: "Ao continuar você ficará devendo. Tem certeza?",
        blockedUserId: args.userId,
        requiredChips: args.requiredChips,
        currentChips: preview.currentChips,
        currentBonusChips: preview.currentBonusChips,
        resultingChips: preview.resultingChips,
        resultingBonusChips: preview.resultingBonusChips,
        bonusToUse: preview.bonusToUse,
      });
    }
    if (preview.reason === "negative_confirm_required") {
      return buildStakeGateResponse({
        error: "negative_confirm_required",
        detail: "Você está negativado. Se continuar, sua dívida vai aumentar. Tem certeza?",
        blockedUserId: args.userId,
        requiredChips: args.requiredChips,
        currentChips: preview.currentChips,
        currentBonusChips: preview.currentBonusChips,
        resultingChips: preview.resultingChips,
        resultingBonusChips: preview.resultingBonusChips,
        bonusToUse: preview.bonusToUse,
      });
    }
    return buildStakeGateResponse({
      error: "insufficient_chips",
      detail: "Você não tem fichas pra continuar",
      blockedUserId: args.userId,
      requiredChips: args.requiredChips,
      currentChips: preview.currentChips,
      currentBonusChips: preview.currentBonusChips,
      resultingChips: preview.resultingChips,
      resultingBonusChips: preview.resultingBonusChips,
      bonusToUse: preview.bonusToUse,
    });
  }

  const handleHealth: RequestHandler = (req, res) => {
    console.log("[sinuca-health]", JSON.stringify({ origin: req.headers.origin ?? null, ua: req.headers["user-agent"] ?? null, url: req.url ?? null }));
    sendNoStoreJson(res, { ok: true, rules: getInitialRuleSet() });
  };

  const handleSession: RequestHandler = (req, res) => {
    console.log("[sinuca-proxy-session]", JSON.stringify({
      hasProxyPayload: Boolean(req.headers["x-discord-proxy-payload"]),
      origin: req.headers.origin ?? null,
      referer: req.headers.referer ?? null,
      url: req.url ?? null,
      ua: req.headers["user-agent"] ?? null,
    }));
    const session = resolveRequestSession(req);
    console.log("[sinuca-proxy-session]", JSON.stringify({
      userId: session.userId,
      displayName: session.displayName,
      guildId: session.guildId,
      channelId: session.channelId,
      instanceId: session.instanceId,
      sessionSource: session.sessionSource,
      proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
      origin: req.headers.origin ?? null,
      referer: req.headers.referer ?? null,
      ua: req.headers["user-agent"] ?? null,
    }));
    res.json({
      ...session,
      proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
      hasProxyPayload: Boolean(req.headers["x-discord-proxy-payload"]),
    });
  };

  const handleTokenRequest: RequestHandler = (req, res) => {
    const bodyCode = typeof req.body?.code === "string" ? req.body.code : "";
    const queryCode = typeof req.query?.code === "string" ? req.query.code : "";
    const code = bodyCode || queryCode;
    const codeSource = bodyCode ? "body" : (queryCode ? "query" : "missing");
    console.log("[sinuca-token-route]", JSON.stringify({
      method: req.method,
      url: req.url ?? null,
      origin: req.headers.origin ?? null,
      referer: req.headers.referer ?? null,
      ua: req.headers["user-agent"] ?? null,
      codeSource,
      hasCode: Boolean(code),
      codePrefix: code ? code.slice(0, 12) : null,
    }));
    void exchangeDiscordCode(code).then((result) => {
      console.log("[sinuca-token-route-result]", JSON.stringify({ ok: result.ok, error: result.error, detail: result.detail }));
      if (!result.ok || !result.accessToken) {
        res.status(result.error === "missing_code" ? 400 : result.error === "oauth_not_configured" ? 500 : 502).json({ error: result.error, detail: result.detail });
        return;
      }
      res.json({ access_token: result.accessToken });
    }).catch((error) => {
      console.error("[sinuca-oauth] token route unhandled error", error);
      res.status(500).json({ error: "token_exchange_exception" });
    });
  };

  async function handleListRoomsHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const payload = {
      mode: normalizeRoomMode(firstString(req.body?.mode) ?? firstString(req.query?.mode), session.guildId),
      guildId: normalizeIntString(firstString(req.body?.guildId) ?? firstString(req.query?.guildId)) ?? session.guildId,
      channelId: normalizeIntString(firstString(req.body?.channelId) ?? firstString(req.query?.channelId)) ?? session.channelId,
    } satisfies ListRoomsPayload;
    const rooms = listRooms(payload).map(toSnapshot);
    console.log("[sinuca-list-rooms-http]", JSON.stringify({ payload, session, rooms: rooms.map((room) => ({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, players: room.players.length, status: room.status, tableType: room.tableType, stakeChips: room.stakeChips })) }));
    sendNoStoreJson(res, { rooms });
  }

  async function handleGetRoomHttp(req: Request, res: Response) {
    const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
    if (!roomId) {
      res.status(400).json({ error: "missing_room_id" });
      return;
    }
    const room = getRoom(roomId);
    console.log("[sinuca-get-room-http]", JSON.stringify({ roomId, found: Boolean(room) }));
    sendNoStoreJson(res, { room: room ? toSnapshot(room) : null });
  }

  async function handleCreateRoomHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const instanceId = normalizeIntString(merged.instanceId);
    const guildId = normalizeIntString(merged.guildId);
    const channelId = normalizeIntString(merged.channelId);
    const userId = normalizeIntString(merged.userId);
    const displayName = firstString(merged.displayName);
    const avatarUrl = firstString(merged.avatarUrl);
    const requestedTableType = merged.tableType === "casual" ? "casual" : "stake";
    const stakeChips = typeof merged.stakeChips === "number" ? merged.stakeChips : Number(merged.stakeChips ?? 0);
    console.log("[sinuca-create-room-http-request]", JSON.stringify({ session, merged: { instanceId, guildId, channelId, userId, displayName, avatarUrl, tableType: requestedTableType, stakeChips } }));
    if (!instanceId || !userId || !displayName) {
      res.status(400).json({ error: "incomplete_session" });
      return;
    }
    const normalizedStake = Number.isFinite(stakeChips) ? Math.max(0, stakeChips) : 0;
    const confirmBonus = booleanish(merged.confirmBonus, false);
    const confirmDebt = booleanish(merged.confirmDebt, false);
    let createPreview: Awaited<ReturnType<typeof balanceService.previewStakeSpend>> | null = null;
    if (requestedTableType === "stake" && normalizedStake > 0 && guildId) {
      createPreview = await balanceService.previewStakeSpend(guildId, userId, normalizedStake);
      const confirmSatisfied = createPreview.reason === "ok"
        || (createPreview.reason === "bonus_confirm_required" && confirmBonus)
        || ((createPreview.reason === "debt_confirm_required" || createPreview.reason === "negative_confirm_required") && confirmDebt);
      if (!createPreview.canProceed || !confirmSatisfied) {
        const payload = await buildStakePreviewResponse({ guildId, userId, requiredChips: normalizedStake });
        console.log("[sinuca-create-room-http-rejected]", JSON.stringify({ roomId: null, userId, guildId, reason: payload.error, requiredChips: normalizedStake, currentChips: payload.currentChips, currentBonusChips: payload.currentBonusChips, resultingChips: payload.resultingChips, resultingBonusChips: payload.resultingBonusChips, bonusToUse: payload.bonusToUse }));
        res.status(409).json(payload);
        return;
      }
    }
    const room = createRoom(instanceId, guildId, channelId, userId, displayName, avatarUrl ?? null, {
      tableType: requestedTableType,
      stakeChips: Number.isFinite(stakeChips) ? stakeChips : null,
    });
    if (createPreview && requestedTableType === "stake" && normalizedStake > 0) {
      applyStakeGateAcceptance(room.roomId, userId, normalizedStake, createPreview);
    }
    runtime.touchRoomActivity(room.roomId, "http_create_room");
    runtime.broadcastRoom(room.roomId);
    runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    sendNoStoreJson(res, { room: toSnapshot(room) });
  }

  async function handleJoinRoomHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    const displayName = firstString(merged.displayName);
    const avatarUrl = firstString(merged.avatarUrl);
    const confirmBonus = booleanish(merged.confirmBonus, false);
    const confirmDebt = booleanish(merged.confirmDebt, false);
    console.log("[sinuca-join-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, displayName, avatarUrl, confirmBonus, confirmDebt } }));
    if (!roomId || !userId || !displayName) {
      res.status(400).json({ error: "missing_join_identifiers" });
      return;
    }
    const existingRoom = getRoom(roomId);
    if (!existingRoom) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    const requiredStake = existingRoom.tableType === "stake" ? Math.max(0, Number(existingRoom.stakeChips ?? 0) || 0) : 0;
    let joinPreview: Awaited<ReturnType<typeof balanceService.previewStakeSpend>> | null = null;
    if (requiredStake > 0 && existingRoom.guildId) {
      joinPreview = await balanceService.previewStakeSpend(existingRoom.guildId, userId, requiredStake);
      const confirmSatisfied = joinPreview.reason === "ok"
        || (joinPreview.reason === "bonus_confirm_required" && confirmBonus)
        || ((joinPreview.reason === "debt_confirm_required" || joinPreview.reason === "negative_confirm_required") && confirmDebt);
      if (!joinPreview.canProceed || !confirmSatisfied) {
        const payload = await buildStakePreviewResponse({ guildId: existingRoom.guildId, userId, requiredChips: requiredStake });
        console.log("[sinuca-join-room-http-rejected]", JSON.stringify({ roomId, userId, guildId: existingRoom.guildId, reason: payload.error, requiredChips: requiredStake, currentChips: payload.currentChips, currentBonusChips: payload.currentBonusChips, resultingChips: payload.resultingChips, resultingBonusChips: payload.resultingBonusChips, bonusToUse: payload.bonusToUse }));
        res.status(409).json(payload);
        return;
      }
    }
    const room = addPlayer(roomId, userId, displayName, avatarUrl ?? null);
    if (!room) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    if (joinPreview && requiredStake > 0) {
      applyStakeGateAcceptance(room.roomId, userId, requiredStake, joinPreview);
    }
    runtime.touchRoomActivity(room.roomId, "http_join_room");
    runtime.broadcastRoom(room.roomId);
    runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    sendNoStoreJson(res, { room: toSnapshot(room) });
  }

  async function handleLeaveRoomHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    const shouldCloseRoom = booleanish(merged.closeRoom, false);
    console.log("[sinuca-leave-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, closeRoom: shouldCloseRoom } }));
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_leave_identifiers" });
      return;
    }
    const previous = getRoom(roomId);
    if (previous?.status === "in_game") {
      removeGame(roomId);
      setRoomInGame(roomId, false);
    }
    const room = shouldCloseRoom && previous && previous.hostUserId === userId
      ? null
      : removePlayer(roomId, userId);
    const closedRoom = shouldCloseRoom && previous && previous.hostUserId === userId
      ? runtime.closeRoomAndNotify(roomId, ROOM_CLOSE_REASONS.hostClosedRoom, "A sala foi fechada pelo anfitrião.")
      : null;
    if (room) {
      runtime.touchRoomActivity(room.roomId, "http_leave_room_remaining");
      runtime.broadcastRoom(room.roomId);
      runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    } else if (!closedRoom && previous) {
      runtime.clearRoomActivity(previous.roomId);
      runtime.dropAimState(previous.roomId);
      runtime.broadcastRoomList({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode });
    }
    sendNoStoreJson(res, { room: room ? toSnapshot(room) : null, closed: Boolean(closedRoom) });
  }

  async function handleReadyRoomHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    const ready = booleanish(merged.ready, false);
    console.log("[sinuca-ready-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, ready } }));
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_ready_identifiers" });
      return;
    }
    const room = setPlayerReady(roomId, userId, ready);
    if (!room) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    runtime.touchRoomActivity(room.roomId, "http_set_ready");
    runtime.broadcastRoom(room.roomId);
    runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    sendNoStoreJson(res, { room: toSnapshot(room) });
  }

  async function handleUpdateStakeRoomHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    const rawStake = typeof merged.stakeChips === "number" ? merged.stakeChips : Number(merged.stakeChips ?? 0);
    const tableType = merged.tableType === "casual" || rawStake === 0 ? "casual" : "stake";
    console.log("[sinuca-stake-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, tableType, stakeChips: rawStake } }));
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_stake_identifiers" });
      return;
    }
    const currentRoom = getRoom(roomId);
    if (!currentRoom) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    if (currentRoom.hostUserId !== userId) {
      res.status(403).json({ error: "only_host_can_update_stake" });
      return;
    }
    const room = setRoomStake(roomId, userId, { tableType, stakeChips: Number.isFinite(rawStake) ? rawStake : null });
    if (!room) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    runtime.touchRoomActivity(room.roomId, "http_update_stake");
    runtime.broadcastRoom(room.roomId);
    runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    sendNoStoreJson(res, { room: toSnapshot(room) });
  }

  async function handleGetAimHttp(req: Request, res: Response) {
    const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
    if (!roomId) {
      res.status(400).json({ error: "missing_room_id" });
      return;
    }
    sendNoStoreJson(res, { aim: runtime.getAimState(roomId) ?? null });
  }

  async function handleSyncAimHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_aim_identifiers" });
      return;
    }
    const room = getRoom(roomId);
    const game = getGameSnapshot(roomId);
    if (!room || !game) {
      res.status(404).json({ error: "game_not_found" });
      return;
    }
    const payload = runtime.buildAimPayload({
      roomId,
      userId,
      visible: Boolean(merged.visible),
      angle: merged.angle,
      cueX: merged.cueX,
      cueY: merged.cueY,
      power: merged.power,
      seq: merged.seq,
      mode: merged.mode,
    });
    if (payload.visible && game.turnUserId !== userId) {
      res.status(409).json({ error: "not_your_turn", aim: runtime.getAimState(roomId) ?? null });
      return;
    }
    runtime.storeAimState(roomId, payload);
    runtime.touchRoomActivity(roomId, "http_sync_aim");
    runtime.broadcastAim(roomId, payload);
    res.json({ aim: payload });
  }

  async function handleGetGameHttp(req: Request, res: Response) {
    const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
    const sinceSeq = Number(firstString(req.body?.sinceSeq) ?? firstString(req.query?.sinceSeq) ?? 0);
    const session = resolveRequestSession(req);
    console.log("[sinuca-game-snapshot-http]", JSON.stringify({
      method: req.method,
      url: req.url ?? null,
      roomId,
      sinceSeq: Number.isFinite(sinceSeq) ? sinceSeq : 0,
      userId: session.userId,
      guildId: session.guildId,
      instanceId: session.instanceId,
    }));
    if (!roomId) {
      console.log("[sinuca-game-snapshot-http-rejected]", JSON.stringify({ reason: "missing_room_id", url: req.url ?? null }));
      res.status(400).json({ error: "missing_room_id" });
      return;
    }
    const game = getGameSnapshot(roomId, Number.isFinite(sinceSeq) ? sinceSeq : 0);
    console.log("[sinuca-game-snapshot-http-result]", JSON.stringify({
      roomId,
      hasGame: Boolean(game),
      gameId: game?.gameId ?? null,
      shotSequence: game?.shotSequence ?? null,
      status: game?.status ?? null,
    }));
    sendNoStoreJson(res, { game });
  }

  async function handleStartGameHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    console.log("[sinuca-start-http]", JSON.stringify({
      method: req.method,
      url: req.url ?? null,
      roomId,
      userId,
      sessionUserId: session.userId,
      sessionGuildId: session.guildId,
    }));
    if (!roomId || !userId) {
      console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "missing_start_identifiers" }));
      res.status(400).json({ error: "missing_start_identifiers" });
      return;
    }
    const room = getRoom(roomId);
    if (!room) {
      console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "room_not_found" }));
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    if (room.hostUserId !== userId) {
      console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "only_host_can_start", hostUserId: room.hostUserId }));
      res.status(403).json({ error: "only_host_can_start" });
      return;
    }
    const opponent = room.players.find((player) => player.userId !== room.hostUserId);
    if (!opponent || !opponent.ready) {
      console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "room_not_ready", opponentUserId: opponent?.userId ?? null, opponentReady: opponent?.ready ?? null }));
      res.status(409).json({ error: "room_not_ready" });
      return;
    }
    const requiredStake = room.tableType === "stake" ? Math.max(0, Number(room.stakeChips ?? 0) || 0) : 0;
    if (requiredStake > 0 && room.guildId) {
      const hostPreview = await balanceService.previewStakeSpend(room.guildId, room.hostUserId, requiredStake);
      const hostPlayer = room.players.find((player) => player.userId === room.hostUserId);
      const hostAccepted = hostPreview.reason === "ok" || acceptanceMatchesPreview(hostPlayer, requiredStake, hostPreview);
      if (!hostPreview.canProceed || !hostAccepted) {
        const payload = !hostPreview.canProceed
          ? await buildStakePreviewResponse({ guildId: room.guildId, userId: room.hostUserId, requiredChips: requiredStake })
          : buildStakeGateResponse({
              error: "stake_confirmation_required",
              detail: "Confirme suas fichas ao criar ou entrar na sala antes de iniciar.",
              blockedUserId: room.hostUserId,
              requiredChips: requiredStake,
              currentChips: hostPreview.currentChips,
              currentBonusChips: hostPreview.currentBonusChips,
              resultingChips: hostPreview.resultingChips,
              resultingBonusChips: hostPreview.resultingBonusChips,
              bonusToUse: hostPreview.bonusToUse,
            });
        console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: payload.error, requiredStake, currentChips: payload.currentChips, currentBonusChips: payload.currentBonusChips, resultingChips: payload.resultingChips, bonusToUse: payload.bonusToUse }));
        res.status(409).json(payload);
        return;
      }
      const opponentPreview = await balanceService.previewStakeSpend(room.guildId, opponent.userId, requiredStake);
      const opponentAccepted = opponentPreview.reason === "ok" || acceptanceMatchesPreview(opponent, requiredStake, opponentPreview);
      if (!opponentPreview.canProceed || !opponentAccepted) {
        const payload = !opponentPreview.canProceed
          ? await buildStakePreviewResponse({ guildId: room.guildId, userId: opponent.userId, requiredChips: requiredStake })
          : buildStakeGateResponse({
              error: "opponent_confirmation_required",
              detail: "O outro jogador precisa confirmar as fichas ao entrar na sala antes de iniciar.",
              blockedUserId: opponent.userId,
              requiredChips: requiredStake,
              currentChips: opponentPreview.currentChips,
              currentBonusChips: opponentPreview.currentBonusChips,
              resultingChips: opponentPreview.resultingChips,
              resultingBonusChips: opponentPreview.resultingBonusChips,
              bonusToUse: opponentPreview.bonusToUse,
            });
        console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: payload.error, requiredStake, blockedUserId: opponent.userId, currentChips: payload.currentChips, currentBonusChips: payload.currentBonusChips, resultingChips: payload.resultingChips, bonusToUse: payload.bonusToUse }));
        res.status(409).json(payload);
        return;
      }
    }
    setRoomInGame(roomId, true);
    const game = startGameForRoom(room);
    runtime.dropAimState(roomId);
    runtime.touchRoomActivity(roomId, "http_start_game");
    runtime.broadcastRoom(roomId);
    runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    runtime.broadcastGame(roomId);
    console.log("[sinuca-start-http-applied]", JSON.stringify({ roomId, userId, turnUserId: game.turnUserId, phase: game.phase, shotSequence: game.shotSequence }));
    res.json({ game, room: toSnapshot(getRoom(roomId) ?? room) });
  }

  async function handleShootGameHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    const angle = Number(merged.angle ?? 0);
    const power = Number(merged.power ?? 0);
    const cueX = merged.cueX === undefined ? null : Number(merged.cueX);
    const cueY = merged.cueY === undefined ? null : Number(merged.cueY);
    const calledPocket = merged.calledPocket === undefined ? null : Number(merged.calledPocket);
    const spinX = merged.spinX === undefined ? 0 : Number(merged.spinX);
    const spinY = merged.spinY === undefined ? 0 : Number(merged.spinY);
    console.log("[sinuca-shoot-http]", JSON.stringify({ method: req.method, session, request: { query: req.query ?? null, body: req.body ?? null }, merged, roomId, userId, angle, power, cueX, cueY, calledPocket, spinX, spinY }));
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_shot_identifiers" });
      return;
    }
    const room = getRoom(roomId);
    if (!room || room.status !== "in_game") {
      res.status(404).json({ error: "game_not_found" });
      return;
    }
    const game = getGameSnapshot(roomId);
    if (!game) {
      res.status(404).json({ error: "game_not_found" });
      return;
    }
    if (game.turnUserId !== userId) {
      res.status(409).json({ error: "not_your_turn", game });
      return;
    }
    const nextGame = takeShotChecked(roomId, userId, angle, power, cueX, cueY, calledPocket, spinX, spinY);
    if (!nextGame.ok || !nextGame.game) {
      const statusCode = nextGame.error === "game_not_found" ? 404 : 409;
      console.log("[sinuca-shoot-http-rejected]", JSON.stringify({ roomId, userId, error: nextGame.error, detail: nextGame.detail ?? null }));
      res.status(statusCode).json({ error: nextGame.error ?? "shot_rejected", detail: nextGame.detail ?? null, game: nextGame.game });
      return;
    }
    const clearedAim = runtime.clearAimState(roomId, userId);
    if (clearedAim) {
      runtime.broadcastAim(roomId, clearedAim);
    }
    runtime.touchRoomActivity(roomId, "http_take_shot");
    console.log("[sinuca-shoot-http-applied]", JSON.stringify({
      roomId,
      shotSequence: nextGame.game.shotSequence,
      turnUserId: nextGame.game.turnUserId,
      phase: nextGame.game.phase,
      status: nextGame.game.status,
      snapshotRevision: nextGame.game.snapshotRevision,
      winnerUserId: nextGame.game.winnerUserId,
      cuePocketed: nextGame.game.lastShot?.cuePocketed ?? null,
      pocketedNumbers: nextGame.game.lastShot?.pocketedNumbers ?? null,
    }));
    runtime.broadcastGame(roomId);
    res.json({ game: nextGame.game });
  }

  async function handleRematchReadyHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const roomId = normalizeIntString(merged.roomId);
    const userId = normalizeIntString(merged.userId);
    console.log("[sinuca-rematch-ready-http]", JSON.stringify({ roomId, userId }));
    if (!roomId || !userId) {
      res.status(400).json({ error: "missing_rematch_identifiers" });
      return;
    }
    const room = getRoom(roomId);
    if (!room) {
      res.status(404).json({ error: "room_not_found" });
      return;
    }
    if (!room.players.some((p) => p.userId === userId)) {
      res.status(403).json({ error: "not_in_room" });
      return;
    }
    if (room.players.length < 2) {
      res.status(409).json({ error: "need_two_players" });
      return;
    }

    // Toggle this player's rematch readiness
    toggleRematchReady(roomId, userId);
    const readyCount = room.rematchReadyUserIds?.length ?? 0;
    console.log("[sinuca-rematch-ready]", JSON.stringify({ roomId, userId, readyCount, readyUserIds: room.rematchReadyUserIds }));

    // If both players are ready, execute the rematch
    if (readyCount >= 2) {
      clearRematchReady(roomId);
      removeGame(roomId);
      for (const player of room.players) { player.ready = true; }
      setRoomInGame(roomId, true);
      const game = startGameForRoom(room);
      runtime.dropAimState(roomId);
      runtime.touchRoomActivity(roomId, "http_rematch");
      runtime.broadcastRoom(roomId);
      runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      runtime.broadcastGame(roomId);
      console.log("[sinuca-rematch-applied]", JSON.stringify({ roomId, userId, gameId: game.gameId, turnUserId: game.turnUserId }));
      sendNoStoreJson(res, { game, room: toSnapshot(room), rematchStarted: true });
      return;
    }

    // Otherwise just broadcast the updated room state so both players see the counter
    runtime.touchRoomActivity(roomId, "http_rematch_ready");
    runtime.broadcastRoom(roomId);
    sendNoStoreJson(res, { room: toSnapshot(room), rematchStarted: false });
  }

  async function handleUiDebugHttp(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
    const stage = firstString(merged.stage) ?? 'unknown';
    const roomId = normalizeIntString(merged.roomId);
    const gameId = firstString(merged.gameId);
    const reason = firstString(merged.reason);
    const note = firstString(merged.note);
    const angle = merged.angle === undefined ? null : Number(merged.angle);
    const power = merged.power === undefined ? null : Number(merged.power);
    const cueX = merged.cueX === undefined ? null : Number(merged.cueX);
    const cueY = merged.cueY === undefined ? null : Number(merged.cueY);
    const shotSequence = merged.shotSequence === undefined ? null : Number(merged.shotSequence);
    const gameStatus = firstString(merged.gameStatus);
    const ballInHandUserId = normalizeIntString(merged.ballInHandUserId);
    console.log('[sinuca-ui-debug]', JSON.stringify({
      method: req.method,
      url: req.url ?? null,
      session,
      merged,
      stage,
      roomId,
      gameId,
      reason,
      note,
      angle,
      power,
      cueX,
      cueY,
      shotSequence,
      gameStatus,
      ballInHandUserId,
      origin: req.headers.origin ?? null,
      referer: req.headers.referer ?? null,
      ua: req.headers['user-agent'] ?? null,
    }));
    sendNoStoreJson(res, { ok: true, stage, roomId, gameId });
  }

  async function handleBalance(req: Request, res: Response) {
    const session = resolveRequestSession(req);
    const action = firstString(req.body?.action) ?? firstString(req.query?.action);
    if (action === BALANCE_ACTIONS.roomsList) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleListRoomsHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomGet) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleGetRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomCreate) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleCreateRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomJoin) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleJoinRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomLeave) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleLeaveRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomReady) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleReadyRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.roomStake) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleUpdateStakeRoomHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameGet) {
      return void handleGetGameHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameAimGet) {
      return void handleGetAimHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameStart) {
      console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
      return void handleStartGameHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameAimSync) {
      return void handleSyncAimHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameShoot) {
      return void handleShootGameHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.gameRematchReady) {
      return void handleRematchReadyHttp(req, res);
    }
    if (action === BALANCE_ACTIONS.uiDebug) {
      return void handleUiDebugHttp(req, res);
    }
    const bodyGuildId = typeof req.body?.guildId === "string" ? req.body.guildId : null;
    const bodyUserId = typeof req.body?.userId === "string" ? req.body.userId : null;
    const queryGuildId = typeof req.query?.guildId === "string" ? req.query.guildId : null;
    const queryUserId = typeof req.query?.userId === "string" ? req.query.userId : null;
    const guildId = bodyGuildId ?? queryGuildId ?? session.guildId;
    const userId = bodyUserId ?? queryUserId ?? session.userId;

    console.log("[sinuca-balance-http]", JSON.stringify({
      method: req.method,
      url: req.url ?? null,
      sessionGuildId: session.guildId,
      sessionUserId: session.userId,
      bodyGuildId,
      bodyUserId,
      queryGuildId,
      queryUserId,
      resolvedGuildId: guildId,
      resolvedUserId: userId,
    }));

    if (!guildId || !userId) {
      const debug = balanceService.buildMissingIdentifiersDebug({
        session,
        guildId: guildId ?? null,
        userId: userId ?? null,
        note: "guildId ou userId ausente no fallback HTTP",
      });
      res.status(200).json({ balance: { chips: 0, bonusChips: 0 }, debug });
      return;
    }

    try {
      const result = await balanceService.fetchBalance(guildId, userId, session);
      res.json({ balance: result.balance, debug: result.debug });
    } catch (error) {
      console.error("[sinuca-balance-http-error]", error);
      const debug = balanceService.buildErrorDebug({
        session,
        guildId,
        userId,
        note: "erro ao buscar saldo via fallback HTTP",
      });
      res.status(200).json({ balance: { chips: 0, bonusChips: 0 }, debug });
    }
  }

  registerGetOnly(app, HEALTH_ROUTE_PATHS, handleHealth);
  registerGetOnly(app, SESSION_ROUTE_PATHS, handleSession);
  registerGetPost(app, TOKEN_ROUTE_PATHS, handleTokenRequest);
  registerGetPost(app, ROOM_ROUTE_PATHS.list, handleListRoomsHttp);
  registerGetOnly(app, ROOM_ROUTE_PATHS.get, handleGetRoomHttp);
  registerGetPost(app, ROOM_ROUTE_PATHS.create, handleCreateRoomHttp);
  registerGetPost(app, ROOM_ROUTE_PATHS.join, handleJoinRoomHttp);
  registerGetPost(app, ROOM_ROUTE_PATHS.leave, handleLeaveRoomHttp);
  registerGetPost(app, ROOM_ROUTE_PATHS.ready, handleReadyRoomHttp);
  registerGetPost(app, ROOM_ROUTE_PATHS.stake, handleUpdateStakeRoomHttp);
  registerGetOnly(app, GAME_ROUTE_PATHS.aimAction, handleGetAimHttp);
  registerGetOnly(app, GAME_ROUTE_PATHS.aimByRoom, handleGetAimHttp);
  registerGetOnly(app, GAME_ROUTE_PATHS.gameByRoom, handleGetGameHttp);
  registerGetOnly(app, GAME_ROUTE_PATHS.legacyGameByRoom, handleGetGameHttp);
  registerGetOnly(app, ROOM_ROUTE_PATHS.roomGame, handleGetGameHttp);
  registerPostOnly(app, GAME_ROUTE_PATHS.start, handleStartGameHttp);
  registerPostOnly(app, GAME_ROUTE_PATHS.aimAction, handleSyncAimHttp);
  registerGetPost(app, GAME_ROUTE_PATHS.shootAction, handleShootGameHttp);
  registerGetPost(app, GAME_ROUTE_PATHS.debug, handleUiDebugHttp);
  registerGetPost(app, BALANCE_ROUTE_PATHS, handleBalance);
}
