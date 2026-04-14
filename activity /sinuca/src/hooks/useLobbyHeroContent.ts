import { useMemo } from "react";
import type { RoomSnapshot } from "../types/activity";

type LobbyScreen = "home" | "create" | "list" | "room" | "game";

type UseLobbyHeroContentArgs = {
  screen: LobbyScreen;
  room: RoomSnapshot | null;
  isServer: boolean;
  isRoomHost: boolean;
  canHostStart: boolean;
  roomOpponentPlayer: { ready: boolean } | null;
  currentPlayerReady: boolean;
  createStake: number;
  formatStakeOptionLabel: (stake: number) => string;
};

export type LobbyHeroContent = {
  eyebrow: string;
  title: string;
  subtitle: string;
  secondaryLabel: { label: string; value: string } | null;
  entryEditable: boolean;
};

export function useLobbyHeroContent({
  screen,
  room,
  isServer,
  isRoomHost,
  canHostStart,
  roomOpponentPlayer,
  currentPlayerReady,
  createStake,
  formatStakeOptionLabel,
}: UseLobbyHeroContentArgs): LobbyHeroContent {
  return useMemo(() => {
    const title = (() => {
      if (screen === "create") return "Criar mesa";
      if (screen === "list") return "Mesas abertas";
      if (screen === "room") {
        if (!roomOpponentPlayer) return "Mesa aberta";
        if (isRoomHost) return canHostStart ? "Mesa pronta" : "Mesa aberta";
        return currentPlayerReady ? "Pronto" : "Mesa encontrada";
      }
      if (screen === "game") return "Mesa em jogo";
      return "Sinuca de Femboy";
    })();

    const subtitle = (() => {
      if (screen === "create") return "Abra a mesa e ajuste a entrada.";
      if (screen === "list") return "Entre em uma mesa aberta.";
      if (screen === "room") {
        if (!room) return "Acompanhe a mesa.";
        if (isRoomHost) {
          if (!roomOpponentPlayer) return "Aguardando jogador.";
          return canHostStart ? "Pronta para iniciar." : "Esperando pronto.";
        }
        return currentPlayerReady ? "Aguardando início." : "Marque pronto.";
      }
      if (screen === "game") return "Acompanhe a partida.";
      return "Crie ou entre em uma mesa.";
    })();

    const eyebrow = (() => {
      if (screen === "create") return "Mesa nova";
      if (screen === "list") return "Salas";
      if (screen === "room") return "Sala";
      if (screen === "game") return "Partida";
      return "Lobby";
    })();

    const secondaryLabel = (() => {
      if (!isServer) return null;
      if (screen === "create") {
        return { label: "Entrada", value: formatStakeOptionLabel(createStake) };
      }
      if ((screen === "room" || screen === "game") && room) {
        return {
          label: "Entrada",
          value: formatStakeOptionLabel(room.tableType === "stake" ? (room.stakeChips ?? 0) : 0),
        };
      }
      return null;
    })();

    const entryEditable = Boolean(isServer && secondaryLabel && (screen === "create" || (screen === "room" && room && isRoomHost)));

    return { eyebrow, title, subtitle, secondaryLabel, entryEditable };
  }, [
    canHostStart,
    createStake,
    currentPlayerReady,
    formatStakeOptionLabel,
    isRoomHost,
    isServer,
    room,
    roomOpponentPlayer,
    screen,
  ]);
}
