import type { GameSnapshot } from '../messages.js';
import type { RoomRecord } from '../rooms.js';
import type { BalanceService } from './balanceService.js';
import type { DiscordMessageService } from './discordMessageService.js';

export interface MatchSettlementService {
  handleFinishedGame(room: RoomRecord, game: GameSnapshot): Promise<void>;
}

export function createMatchSettlementService(args: {
  balanceService: BalanceService;
  discordMessageService: DiscordMessageService;
}): MatchSettlementService {
  const completedGameIds = new Set<string>();
  const inflightGameIds = new Set<string>();

  return {
    async handleFinishedGame(room, game) {
      if (game.status !== 'finished' || !game.winnerUserId || !game.gameId) return;
      if (completedGameIds.has(game.gameId) || inflightGameIds.has(game.gameId)) return;
      inflightGameIds.add(game.gameId);
      try {
        const winnerUserId = game.winnerUserId;
        const loserUserId = [game.hostUserId, game.guestUserId].find((userId) => userId && userId !== winnerUserId) ?? null;
        const stakeChips = Math.max(0, Number(room.stakeChips ?? game.stakeChips ?? 0) || 0);
        const isStakeTable = room.tableType === 'stake' && stakeChips > 0;

        if (isStakeTable && room.guildId && loserUserId) {
          await args.balanceService.applyMatchStakeSettlement({
            guildId: room.guildId,
            winnerUserId,
            loserUserId,
            stakeChips,
          });
        }

        try {
          await args.discordMessageService.sendMatchResult({
            channelId: room.channelId,
            winnerUserId,
            loserUserId,
            stakeChips,
            tableType: isStakeTable ? 'stake' : 'casual',
          });
        } catch (error) {
          console.error('[sinuca-match-message] failed', JSON.stringify({ roomId: room.roomId, gameId: game.gameId, channelId: room.channelId, error: error instanceof Error ? error.message : String(error) }));
        }

        completedGameIds.add(game.gameId);
      } finally {
        inflightGameIds.delete(game.gameId);
      }
    },
  };
}
