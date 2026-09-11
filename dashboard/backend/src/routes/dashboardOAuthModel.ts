export function buildDiscordOAuthAuthorizeUrl(clientId: string, redirectUri: string, state: string): string {
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: "identify guilds",
    state,
    prompt: "consent",
  });
  return `https://discord.com/oauth2/authorize?${params.toString()}`;
}
