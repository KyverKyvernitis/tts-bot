import type {
  AimStateSnapshot,
  BalanceDebugSnapshot,
  BalanceSnapshot,
  GameSnapshot,
  RoomSnapshot,
  SessionContextPayload,
} from "../types/activity";
import type { OAuthExchangeResult } from "../transport/sessionApi";

export type IncomingMessage =
  | { type: "ready" }
  | { type: "pong" }
  | { type: "error"; message: string }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] }
  | { type: "game_state"; payload: GameSnapshot }
  | { type: "balance_state"; payload: BalanceSnapshot }
  | { type: "balance_debug"; payload: BalanceDebugSnapshot }
  | { type: "session_context"; payload: SessionContextPayload }
  | { type: "oauth_token_result"; payload: OAuthExchangeResult }
  | { type: "aim_state"; payload: AimStateSnapshot }
  | { type: "room_closed"; payload: { roomId: string; reason: string; message: string } };

export type { OAuthExchangeResult };
