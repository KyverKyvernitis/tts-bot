import express from "express";
import { createServer } from "http";
import { WebSocketServer } from "ws";
import {
  addPlayer,
  createRoom,
  getRoom,
  getSubscribers,
  listRooms,
  removePlayer,
  setPlayerReady,
  subscribeSocket,
  toSnapshot,
  unsubscribeSocket,
} from "./rooms.js";
import type { ClientMessage, ServerMessage } from "./messages.js";
import { getInitialRuleSet } from "./gameRules.js";

const app = express();
app.get("/health", (_req, res) => {
  res.json({ ok: true, rules: getInitialRuleSet() });
});

const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });

function send(ws: import("ws").WebSocket, payload: ServerMessage) {
  ws.send(JSON.stringify(payload));
}

function broadcastRoom(roomId: string) {
  const room = getRoom(roomId);
  if (!room) return;
  const payload: ServerMessage = { type: "room_state", payload: toSnapshot(room) };
  for (const client of getSubscribers(roomId)) {
    send(client, payload);
  }
}

wss.on("connection", (ws) => {
  send(ws, { type: "ready" });

  ws.on("message", (raw) => {
    let data: ClientMessage;
    try {
      data = JSON.parse(raw.toString()) as ClientMessage;
    } catch {
      send(ws, { type: "error", message: "payload inválido" });
      return;
    }

    if (data.type === "ping") {
      send(ws, { type: "pong" });
      return;
    }

    if (data.type === "list_rooms") {
      send(ws, { type: "room_list", payload: listRooms(data.payload).map(toSnapshot) });
      return;
    }

    if (data.type === "create_room") {
      const { instanceId, guildId, channelId, userId, displayName } = data.payload;
      const room = createRoom(instanceId, guildId, channelId, userId, displayName);
      subscribeSocket(room.roomId, ws);
      send(ws, { type: "room_state", payload: toSnapshot(room) });
      send(ws, {
        type: "room_list",
        payload: listRooms({ guildId, channelId, mode: room.mode }).map(toSnapshot),
      });
      return;
    }

    if (data.type === "join_room") {
      const room = addPlayer(data.payload.roomId, data.payload.userId, data.payload.displayName);
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      subscribeSocket(room.roomId, ws);
      broadcastRoom(room.roomId);
      return;
    }

    if (data.type === "leave_room") {
      const previous = getRoom(data.payload.roomId);
      const room = removePlayer(data.payload.roomId, data.payload.userId);
      unsubscribeSocket(ws);
      if (room) {
        broadcastRoom(room.roomId);
        send(ws, {
          type: "room_list",
          payload: listRooms({ guildId: room.guildId, channelId: room.channelId, mode: room.mode }).map(toSnapshot),
        });
      } else if (previous) {
        send(ws, {
          type: "room_list",
          payload: listRooms({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode }).map(toSnapshot),
        });
      }
      return;
    }

    if (data.type === "set_ready") {
      const room = setPlayerReady(data.payload.roomId, data.payload.userId, data.payload.ready);
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      broadcastRoom(room.roomId);
    }
  });

  ws.on("close", () => {
    unsubscribeSocket(ws);
  });
});

const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[sinuca-server] ouvindo na porta ${port}`);
});
