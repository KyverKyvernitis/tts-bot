import { DashboardHttpError } from "../transport/httpClient";

const DASHBOARD_ERROR_MESSAGES: Record<string, string> = {
  session_required: "Sua sessão expirou. Entre novamente com o Discord.",
  session_invalid: "Sua sessão do Discord não é mais válida.",
  access_denied: "Sua conta não tem permissão para configurar este servidor.",
  rate_limited: "Muitas solicitações em pouco tempo. Aguarde um momento.",
  session_store_unavailable: "O serviço de sessões está temporariamente indisponível.",
  discord_unavailable: "O Discord está temporariamente indisponível. Tente novamente em instantes.",
  origin_denied: "A origem desta solicitação não foi autorizada.",
};

export function errorText(error: unknown): string {
  if (error instanceof DashboardHttpError) {
    const key = typeof error.payload === "object" && error.payload
      ? String((error.payload as Record<string, unknown>).error || "")
      : "";
    return DASHBOARD_ERROR_MESSAGES[key] || error.message;
  }
  return error instanceof Error ? error.message : "Ocorreu uma falha inesperada.";
}
