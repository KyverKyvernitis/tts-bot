import { MongoClient, type Collection, type Document } from "mongodb";
import type {
  DashboardSessionStore,
  NewStoredDashboardSession,
  StoredDashboardSession,
  StoredDashboardSessionTokenUpdate,
} from "./dashboardSessionTypes.js";

interface MongoStoredDashboardSession extends Document {
  type: "dashboard_session";
  session_hash: string;
  access_token: string;
  refresh_token: string | null;
  access_expires_at: Date | null;
  expires_at: Date;
  created_at: Date;
  updated_at: Date;
}

export interface CreateMongoDashboardSessionStoreOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName?: string;
}

export function createMongoDashboardSessionStore(options: CreateMongoDashboardSessionStoreOptions): DashboardSessionStore {
  let client: MongoClient | null = null;
  let collection: Collection<MongoStoredDashboardSession> | null = null;
  let indexesReady = false;

  async function getCollection(): Promise<Collection<MongoStoredDashboardSession>> {
    if (!options.mongoUri) throw new Error("mongodb_not_configured");
    if (!collection) {
      client = new MongoClient(options.mongoUri);
      await client.connect();
      collection = client
        .db(options.mongoDbName)
        .collection<MongoStoredDashboardSession>(options.mongoCollectionName || "dashboard_sessions");
    }
    if (!indexesReady) {
      await Promise.all([
        collection.createIndex({ session_hash: 1 }, { unique: true, name: "dashboard_session_hash" }),
        collection.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0, name: "dashboard_session_ttl" }),
      ]);
      indexesReady = true;
    }
    return collection;
  }

  return {
    async insert(session: NewStoredDashboardSession) {
      const coll = await getCollection();
      await coll.insertOne({
        type: "dashboard_session",
        session_hash: session.sessionHash,
        access_token: session.encryptedAccessToken,
        refresh_token: session.encryptedRefreshToken,
        access_expires_at: session.accessExpiresAt,
        expires_at: session.expiresAt,
        created_at: session.createdAt,
        updated_at: session.updatedAt,
      });
    },

    async findByHash(sessionHash: string): Promise<StoredDashboardSession | null> {
      const coll = await getCollection();
      const doc = await coll.findOne({ type: "dashboard_session", session_hash: sessionHash });
      if (!doc) return null;
      return {
        id: doc._id,
        sessionHash: doc.session_hash,
        encryptedAccessToken: doc.access_token,
        encryptedRefreshToken: doc.refresh_token,
        accessExpiresAt: doc.access_expires_at,
        expiresAt: doc.expires_at,
        createdAt: doc.created_at,
        updatedAt: doc.updated_at,
      };
    },

    async deleteById(id: unknown) {
      const coll = await getCollection();
      await coll.deleteOne({ _id: id } as Document);
    },

    async deleteByHash(sessionHash: string) {
      const coll = await getCollection();
      await coll.deleteOne({ type: "dashboard_session", session_hash: sessionHash });
    },

    async updateTokens(id: unknown, update: StoredDashboardSessionTokenUpdate): Promise<boolean> {
      const coll = await getCollection();
      const result = await coll.updateOne(
        { _id: id } as Document,
        {
          $set: {
            access_token: update.encryptedAccessToken,
            refresh_token: update.encryptedRefreshToken,
            access_expires_at: update.accessExpiresAt,
            updated_at: update.updatedAt,
          },
        },
      );
      return result.matchedCount === 1;
    },
  };
}
