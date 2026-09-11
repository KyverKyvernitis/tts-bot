export function guildIconUrl(guildId: string, iconHash: unknown): string | null {
  if (typeof iconHash !== "string" || !iconHash.trim()) return null;
  const extension = iconHash.startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/icons/${guildId}/${iconHash}.${extension}?size=128`;
}

export function discordAvatarUrl(userId: unknown, avatarHash: unknown): string | null {
  if (typeof avatarHash !== "string" || !avatarHash.trim()) return null;
  const id = String(userId ?? "").trim();
  if (!id) return null;
  const extension = avatarHash.startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/avatars/${id}/${avatarHash}.${extension}?size=128`;
}

export function withAvatarUrl<T extends { id: string; avatar?: string | null }>(user: T | null): (T & { avatarUrl: string | null }) | null {
  if (!user) return null;
  return { ...user, avatarUrl: discordAvatarUrl(user.id, user.avatar) };
}

export async function mapWithConcurrency<T, R>(items: T[], limit: number, mapper: (item: T, index: number) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let nextIndex = 0;
  const workers = Array.from({ length: Math.min(Math.max(1, limit), items.length) }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await mapper(items[index]!, index);
    }
  });
  await Promise.all(workers);
  return results;
}
