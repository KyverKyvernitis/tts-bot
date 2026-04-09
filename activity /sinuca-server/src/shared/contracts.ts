export const HEALTH_ROUTE_PATHS = ["/health", "/api/health"] as const;
export const SESSION_ROUTE_PATHS = ["/session", "/api/session"] as const;
export const TOKEN_ROUTE_PATHS = ["/token", "/api/token"] as const;
export const BALANCE_ROUTE_PATHS = ["/balance", "/api/balance"] as const;

export const ROOM_ROUTE_PATHS = {
  list: ["/rooms", "/api/rooms"] as const,
  get: ["/rooms/:roomId", "/api/rooms/:roomId"] as const,
  create: ["/rooms/create", "/api/rooms/create"] as const,
  join: ["/rooms/join", "/api/rooms/join"] as const,
  leave: ["/rooms/leave", "/api/rooms/leave"] as const,
  ready: ["/rooms/ready", "/api/rooms/ready"] as const,
  stake: ["/rooms/stake", "/api/rooms/stake"] as const,
  roomGame: ["/rooms/:roomId/game", "/api/rooms/:roomId/game"] as const,
} as const;

export const GAME_ROUTE_PATHS = {
  shootAction: ["/games/shoot", "/api/games/shoot"] as const,
  aimAction: ["/games/aim", "/api/games/aim"] as const,
  aimByRoom: ["/games/:roomId/aim", "/api/games/:roomId/aim"] as const,
  gameByRoom: ["/games/:roomId", "/api/games/:roomId"] as const,
  legacyGameByRoom: ["/game/:roomId", "/api/game/:roomId"] as const,
  start: ["/games/start", "/api/games/start"] as const,
  debug: ["/games/debug", "/api/games/debug"] as const,
} as const;

export const BALANCE_ACTIONS = {
  roomsList: "rooms_list",
  roomGet: "room_get",
  roomCreate: "room_create",
  roomJoin: "room_join",
  roomLeave: "room_leave",
  roomReady: "room_ready",
  roomStake: "room_stake",
  gameGet: "game_get",
  gameAimGet: "game_aim_get",
  gameStart: "game_start",
  gameAimSync: "game_aim_sync",
  gameShoot: "game_shoot",
  gameRematchReady: "game_rematch_ready",
  uiDebug: "ui_debug",
} as const;

export const ROOM_CLOSE_REASONS = {
  hostClosedRoom: "host_closed_room",
  idleTimeout: "idle_timeout",
} as const;
