import express from "express";
import { createServer } from "http";
import { WebSocketServer } from "ws";
import { addPlayer, getOrCreateRoom, subscribeSocket, toSnapshot, unsubscribeSocket } from "./rooms.js";
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

    if (data.type === "create_room") {
      const { instanceId, guildId, channelId } = data.payload;
      const room = getOrCreateRoom(instanceId, guildId, channelId);
      subscribeSocket(instanceId, ws);
      send(ws, { type: "room_state", payload: toSnapshot(room) });
      return;
    }

    if (data.type === "join_room") {
      const { instanceId, userId, displayName } = data.payload;
      const room = addPlayer(instanceId, userId, displayName);
      if (!room) {
        send(ws, { type: "error", message: "sala não encontrada" });
        return;
      }
      const payload: ServerMessage = { type: "room_state", payload: toSnapshot(room) };
      for (const client of subscribeSocket(instanceId, ws)) {
        send(client, payload);
      }
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
