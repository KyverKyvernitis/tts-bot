import { sendSocketMessage } from "./socketClient";

export function sendSubscribeRoomMessage(params: {
  socket: WebSocket | null | undefined;
  roomId: string;
  userId: string;
}) {
  return sendSocketMessage(params.socket, {
    type: "subscribe_room",
    payload: {
      roomId: params.roomId,
      userId: params.userId,
    },
  });
}
