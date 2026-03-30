export interface SinucaRuleSet {
  table: "8-ball-simplificada";
  turnTimeSeconds: number;
  winCondition: string;
  foulRule: string;
}

export function getInitialRuleSet(): SinucaRuleSet {
  return {
    table: "8-ball-simplificada",
    turnTimeSeconds: 30,
    winCondition: "encaçapar a 8 no momento correto vence a partida",
    foulRule: "falta passa a vez e abre espaço para regras específicas nos próximos patches",
  };
}
