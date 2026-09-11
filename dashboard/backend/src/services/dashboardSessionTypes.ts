export interface DashboardOAuthTokenResult {
  ok: boolean;
  accessToken: string | null;
  refreshToken?: string | null;
  expiresIn?: number | null;
  error: string | null;
  detail: string | null;
}

export interface DashboardSession {
  id: string;
  accessToken: string;
  refreshToken: string | null;
  accessExpiresAt: number | null;
  expiresAt: number;
}

export interface StoredDashboardSession {
  id: unknown;
  sessionHash: string;
  encryptedAccessToken: string;
  encryptedRefreshToken: string | null;
  accessExpiresAt: Date | null;
  expiresAt: Date;
  createdAt: Date;
  updatedAt: Date;
}

export interface NewStoredDashboardSession extends Omit<StoredDashboardSession, "id"> {}

export interface StoredDashboardSessionTokenUpdate {
  encryptedAccessToken: string;
  encryptedRefreshToken: string | null;
  accessExpiresAt: Date | null;
  updatedAt: Date;
}

export interface DashboardSessionStore {
  insert(session: NewStoredDashboardSession): Promise<void>;
  findByHash(sessionHash: string): Promise<StoredDashboardSession | null>;
  deleteById(id: unknown): Promise<void>;
  deleteByHash(sessionHash: string): Promise<void>;
  updateTokens(id: unknown, update: StoredDashboardSessionTokenUpdate): Promise<boolean>;
}
