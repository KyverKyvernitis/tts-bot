export interface ActivityMatchMessagePayload {
  channelId: string | null;
  winnerUserId: string;
  loserUserId?: string | null;
  stakeChips?: number | null;
  tableType: "stake" | "casual";
}

export interface DiscordMessageService {
  sendMatchResult(payload: ActivityMatchMessagePayload): Promise<void>;
}

const COMPONENTS_V2_FLAG = 1 << 15;
const CONTAINER = 17;
const TEXT_DISPLAY = 10;

function buildStakeMessage(payload: ActivityMatchMessagePayload) {
  const stake = Math.max(0, Number(payload.stakeChips ?? 0) || 0);
  return {
    flags: COMPONENTS_V2_FLAG,
    allowed_mentions: {
      users: [payload.winnerUserId, payload.loserUserId].filter((value): value is string => Boolean(value)),
    },
    components: [
      {
        type: CONTAINER,
        accent_color: 0x57F287,
        components: [
          {
            type: TEXT_DISPLAY,
            content: [
              '# 🎱 Resultado da sinuca',
              `🏆 <@${payload.winnerUserId}> venceu a mesa e ganhou **+${stake} fichas**.`,
              payload.loserUserId ? `💸 <@${payload.loserUserId}> perdeu **${stake} fichas**.` : null,
              `💠 Mesa valendo **${stake} fichas**.`,
            ].filter(Boolean).join('\n'),
          },
        ],
      },
    ],
  };
}

function buildCasualMessage(payload: ActivityMatchMessagePayload) {
  return {
    flags: COMPONENTS_V2_FLAG,
    allowed_mentions: {
      users: [payload.winnerUserId].filter((value): value is string => Boolean(value)),
    },
    components: [
      {
        type: CONTAINER,
        accent_color: 0x5865f2,
        components: [
          {
            type: TEXT_DISPLAY,
            content: [
              '# 🎱 Partida encerrada',
              `🏆 Vencedor: <@${payload.winnerUserId}>`,
              'Mesa amistosa finalizada.',
            ].join('\n'),
          },
        ],
      },
    ],
  };
}

export function createDiscordMessageService(): DiscordMessageService {
  const botToken = (process.env.DISCORD_TOKEN || process.env.BOT_TOKEN || '').trim();

  async function postChannelMessage(channelId: string, body: Record<string, unknown>) {
    if (!botToken) {
      throw new Error('missing_discord_token');
    }
    const response = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
      method: 'POST',
      headers: {
        Authorization: `Bot ${botToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    const raw = await response.text();
    if (!response.ok) {
      throw new Error(`discord_message_failed:${response.status}:${raw.slice(0, 240) || 'empty'}`);
    }
  }

  return {
    async sendMatchResult(payload: ActivityMatchMessagePayload) {
      if (!payload.channelId) return;
      const body = payload.tableType === 'stake' && Number(payload.stakeChips ?? 0) > 0
        ? buildStakeMessage(payload)
        : buildCasualMessage(payload);
      await postChannelMessage(payload.channelId, body);
    },
  };
}
