import type { RoomPlayer, RoomSnapshot } from "../types/activity";

export function formatStatus(room: RoomSnapshot) {
  if (room.status === "ready") return "pronta";
  if (room.status === "in_game") return "em jogo";
  return "aguardando";
}

export function formatRoomCount(count: number) {
  return count === 1 ? "1 aberta" : `${count} abertas`;
}

function defaultDiscordAvatarUrl(userId: string) {
  try {
    const index = Number((BigInt(userId) >> 22n) % 6n);
    return `https://cdn.discordapp.com/embed/avatars/${index}.png`;
  } catch {
    return "https://cdn.discordapp.com/embed/avatars/0.png";
  }
}

export function cleanPlayerName(player: Pick<RoomPlayer, "displayName">) {
  const label = player.displayName?.trim() || "jogador";
  return label.replace(/^@+/, "");
}

export function resolvePlayerAvatar(player: Pick<RoomPlayer, "userId" | "avatarUrl">) {
  if (player.avatarUrl) return player.avatarUrl;
  return defaultDiscordAvatarUrl(player.userId);
}
