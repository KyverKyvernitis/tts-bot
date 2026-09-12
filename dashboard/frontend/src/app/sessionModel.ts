import { DashboardHttpError } from "../transport/httpClient";
import type { DashboardUserPayload } from "../types/dashboard";
import { errorText } from "./errors";

export type BootSessionResult = {
  state: "authenticated" | "anonymous";
  user: DashboardUserPayload | null;
  notice?: { type: "error"; text: string };
};

export function sessionResultFromError(error: unknown): BootSessionResult {
  if (error instanceof DashboardHttpError && error.status === 401) {
    return { state: "anonymous", user: null };
  }
  return { state: "anonymous", user: null, notice: { type: "error", text: errorText(error) } };
}
