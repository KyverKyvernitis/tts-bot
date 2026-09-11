const DEFAULT_COLOR_SLOT_DATA: Array<[number, string, string, string]> = [
  [1, "Vermelho escuro", "#B11212", "#8B0000"], [2, "Amarelo escuro", "#C9A31A", "#B8860B"],
  [3, "Verde escuro", "#0B5D30", "#006400"], [4, "Azul escuro", "#1737D8", "#00008B"],
  [5, "Rosa escuro", "#D61EA6", "#C71585"], [6, "Roxo escuro", "#9A0EC7", "#800080"],
  [7, "Laranja escuro", "#D98900", "#FF8C00"], [8, "Bege escuro", "#B96D43", "#A0522D"],
  [9, "Ciano escuro", "#008F98", "#008B8B"], [10, "Preto", "#000000", "#1F1F1F"],
  [11, "Vermelho", "#FF1B1B", "#FF0000"], [12, "Amarelo", "#FFEC1A", "#FFD700"],
  [13, "Verde", "#11B611", "#00FF00"], [14, "Azul", "#0E2FFF", "#1E90FF"],
  [15, "Rosa", "#FF62C3", "#FF69B4"], [16, "Roxo", "#C020FF", "#9370DB"],
  [17, "Laranja", "#FFAD13", "#FFA500"], [18, "Bege", "#D6B694", "#F5DEB3"],
  [19, "Ciano", "#00ECFF", "#00FFFF"], [20, "Cinza", "#8F8F8F", "#808080"],
  [21, "Vermelho claro", "#FF8B8B", "#FF7F7F"], [22, "Amarelo claro", "#FFF38F", "#FFF68F"],
  [23, "Verde claro", "#9CFF9C", "#90EE90"], [24, "Azul claro", "#A6C7FF", "#87CEFA"],
  [25, "Rosa claro", "#FFB6D9", "#FFB6C1"], [26, "Roxo claro", "#D6A5FF", "#D8BFD8"],
  [27, "Laranja claro", "#FFD199", "#FFCC99"], [28, "Bege claro", "#FFE8D0", "#F5F5DC"],
  [29, "Ciano claro", "#D6FFFF", "#E0FFFF"], [30, "Branco", "#FFFFFF", "#FFFFFF"],
];

export function defaultColorSlots(): Record<string, unknown> {
  return Object.fromEntries(DEFAULT_COLOR_SLOT_DATA.map(([number, name, textHex, roleHex]) => [String(number), {
    number, name, text_hex: textHex.toLowerCase(), role_hex: roleHex.toLowerCase(), role_id: 0, role_name: name, managed: false,
  }]));
}

export function defaultColorPanelLayout(): Array<{ id: string; slots: number[] }> {
  return [0, 1, 2].map((index) => ({
    id: `panel-${index + 1}`,
    slots: Array.from({ length: 10 }, (_, offset) => index * 10 + offset + 1),
  }));
}
