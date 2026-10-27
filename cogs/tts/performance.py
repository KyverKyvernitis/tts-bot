"""Comandos curtos do TTV e identidade de uma apresentação.

O ``~`` ativo no final da mensagem solicita canto e também sustenta a vogal.
Sem esse marcador, a melodia salva não muda a fala.

Os efeitos valem para palavras: ``<olá`` entra suavemente, ``olá>`` sai
suavemente e, dentro do texto, ``olá~ mundo`` sustenta a última vogal de
``olá``. ``|`` separa trechos. ``~`` pode aparecer entre sílabas, junto
de outros ``~`` ou separado: cada símbolo preserva sua sustentação.

Texto sem comandos mantém a rota de fala existente. Menções, URLs, código,
trechos entre aspas duplas e símbolos escapados não viram comandos.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


MAX_EFFECT_SPEECH_CHARACTERS = 180
MAX_PERFORMANCE_SEGMENTS = 128
PAUSE_MILLISECONDS = 180
DEFAULT_MELODY_PRESET = "ttv-demo-v1"


def singing_request(text: str) -> tuple[str, bool]:
    """Only an active final ~ opts a message into singing.

    The suffix both selects singing and requests its own vowel continuation.
    Keep it and the original escaping for the later performance parser; the
    literal mask is used only to decide whether the final character is active.
    A saved melody chooses the song after this opt-in, never speaking mode.
    """
    normalized = _normalize(text)
    literal_text, mask = _literal_characters(normalized)
    if not literal_text.endswith("~") or mask[-1]:
        return text, False
    clean = normalized
    # Keep the old directive as optional wording, but it cannot activate
    # singing without the terminal marker.
    directive = re.match(r"(?i)^/cantar(?:\s+|$)", literal_text)
    if directive and not any(mask[:directive.end()]):
        clean = clean[directive.end():].strip()
    return clean, True

_DISCORD_LITERAL = re.compile(
    r"<(?:@!?|@&|#)\d+>|<a?:[A-Za-z0-9_]+:\d+>|<t:\d+(?::[tTdDfFR])?>"
)
_URL_LITERAL = re.compile(r"<?(?:https?://|www\.)\S+", re.IGNORECASE)
_QUOTE_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|“[^”]*”')
_COMPACT_COMPARISON = re.compile(r"[\w.,+-]+[<>]=?[\w.,+-]+\Z")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _literal_characters(text: str) -> tuple[str, list[bool]]:
    """Remove command escapes while retaining a mask for literal content."""
    protected = [False] * len(text)
    for pattern in (_DISCORD_LITERAL, _URL_LITERAL, _QUOTE_LITERAL):
        for match in pattern.finditer(text):
            protected[match.start():match.end()] = [True] * len(match.group())

    # An unfinished code block is still literal; punctuation inside it must
    # never silently turn into a voice control.
    offset = 0
    while offset < len(text):
        if text[offset] != "`" or protected[offset]:
            offset += 1
            continue
        end = offset
        while end < len(text) and text[end] == "`":
            end += 1
        fence = text[offset:end]
        closing = text.find(fence, end)
        end = len(text) if closing < 0 else closing + len(fence)
        protected[offset:end] = [True] * (end - offset)
        offset = end

    result: list[str] = []
    literals: list[bool] = []
    offset = 0
    while offset < len(text):
        if (text[offset] == "\\" and not protected[offset]
                and offset + 1 < len(text) and not protected[offset + 1]
                and text[offset + 1] in "<>|~\\"):
            result.append(text[offset + 1])
            literals.append(True)
            offset += 2
        else:
            result.append(text[offset])
            literals.append(protected[offset])
            offset += 1
    return "".join(result), literals


def _segment(text: str, *, fade_in: bool = False, fade_out: bool = False,
             hold: bool = False) -> dict[str, Any]:
    return {
        "text": text,
        "fade_in": fade_in,
        "fade_out": fade_out,
        "hold": hold,
        "pause_after_ms": 0,
    }


def parse_performance_text(text: str, *, singing: bool = False
                           ) -> tuple[str, list[dict[str, Any]] | None]:
    """Parse TTV effects without changing ordinary speech requests.

    The clean text equals the space-separated segment texts. A singing
    boundary keeps its ``|`` token and marks the preceding segment with a
    pause, which the singing planner treats only as a phrase boundary.
    Plain text is not subject to the effect limit: existing speech chunking
    continues to handle long messages before synthesis.
    """
    if not isinstance(text, str):
        raise ValueError("O texto do TTV deve ser uma mensagem de texto.")
    text, literals = _literal_characters(_normalize(text))
    units: list[tuple[str, list[bool]]] = []
    for match in re.finditer(r"\S+", text):
        start = match.start()
        part_start = start
        for offset in range(start, match.end()):
            if text[offset] == "|" and not literals[offset]:
                if part_start < offset:
                    units.append((text[part_start:offset], literals[part_start:offset]))
                units.append(("|", [False]))
                part_start = offset + 1
        if part_start < match.end():
            units.append((text[part_start:match.end()], literals[part_start:match.end()]))

    result: list[dict[str, Any]] = []
    has_effects = False
    has_literal_music_marker = False
    boundary_pending = False
    for index, (token, mask) in enumerate(units):
        active = {i for i, char in enumerate(token)
                  if char in "<>|~" and not mask[i]}
        # A neutral musical symbol needs a segment identity in singing.
        # Otherwise a later lyric planner could mistake an escaped/code
        # literal for a new note hold or a phrase boundary from plain text.
        if singing and token in {"~", "|"} and not active:
            has_literal_music_marker = True
        if token == "|" and active:
            if not result or boundary_pending or index == len(units) - 1:
                raise ValueError("Use | entre duas palavras, como: olá | tudo bem.")
            result[-1]["pause_after_ms"] = PAUSE_MILLISECONDS
            if singing:
                result.append(_segment("|"))
            boundary_pending = True
            has_effects = True
            continue

        if token and set(token) == {"~"} and len(active) == len(token):
            if not result or boundary_pending:
                raise ValueError("Use ~ depois de uma sílaba para continuar sua vogal.")
            result.extend(_segment("~", hold=True) for _ in token)
            has_effects = True
            continue

        # Spaced/compact mathematical comparisons remain ordinary text.
        comparison = (token in {"<", ">"} and 0 < index < len(units) - 1
                      and units[index - 1][0] not in {"|", "<", ">", "~"}
                      and units[index + 1][0] not in {"|", "<", ">", "~"})
        if comparison or _COMPACT_COMPARISON.fullmatch(token):
            result.append(_segment(token))
            boundary_pending = False
            continue

        left = 0
        right = len(token)
        # Keep sentence punctuation attached to the word: ``olá~!`` and
        # ``olá>!`` are as practical as ``olá!~`` and ``olá!>``.
        while right > left and not mask[right - 1] and token[right - 1] in ".,!?;:)]}":
            right -= 1
        punctuation = token[right:]
        fade_in = bool(left in active and token[left] == "<")
        if fade_in:
            left += 1
        fade_out = False
        holds = 0
        while right > left and right - 1 in active and token[right - 1] in ">~":
            marker = token[right - 1]
            if marker == ">":
                if fade_out:
                    raise ValueError("Use apenas um > no final da palavra, como: olá>.")
                fade_out = True
            else:
                holds += 1
            right -= 1

        if any(left <= offset < right and token[offset] != "~" for offset in active):
            raise ValueError("Use < antes da palavra, > ou ~ depois, e | entre palavras.")

        # Keep the first attached hold on its word for the existing wire
        # format. Extra symbols become ordinary held segments, so compact
        # and spaced runs retain the same number of requested continuations.
        pieces: list[list[Any]] = []
        part_start = left
        for offset in range(left, right):
            if offset not in active or token[offset] != "~":
                continue
            if part_start < offset:
                pieces.append([token[part_start:offset], 0])
            if pieces:
                pieces[-1][1] += 1
            else:
                # ``a ~mor`` is as valid as ``a~mor``; a leading marker
                # still needs a preceding syllable and cannot cross ``|``.
                if not result or boundary_pending:
                    raise ValueError("Use ~ depois de uma sílaba para continuar sua vogal.")
                result.append(_segment("~", hold=True))
                has_effects = True
            part_start = offset + 1
        if part_start < right:
            pieces.append([token[part_start:right], 0])
        if not pieces:
            if fade_in or fade_out or holds or active:
                raise ValueError("O efeito precisa de uma palavra: <olá, olá> ou olá~.")
            pieces.append(["", 0])
        pieces[-1][0] += punctuation
        pieces[-1][1] += holds
        if (fade_in or fade_out or holds or active) and not any(
                char.isalnum() for word, _ in pieces for char in word):
            raise ValueError("O efeito precisa de uma palavra: <olá, olá> ou olá~.")
        for number, (word, count) in enumerate(pieces):
            result.append(_segment(word, fade_in=fade_in and number == 0,
                                   fade_out=fade_out and number == len(pieces) - 1,
                                   hold=bool(count)))
            result.extend(_segment("~", hold=True) for _ in range(max(0, count - 1)))
        has_effects = has_effects or fade_in or fade_out or any(count for _, count in pieces)
        boundary_pending = False

    clean_text = " ".join(segment["text"] for segment in result)
    if not has_effects and not has_literal_music_marker:
        return clean_text, None
    if len(result) > MAX_PERFORMANCE_SEGMENTS:
        raise ValueError(f"Use até {MAX_PERFORMANCE_SEGMENTS} trechos por apresentação do TTV.")
    if not singing and len(clean_text) > MAX_EFFECT_SPEECH_CHARACTERS:
        raise ValueError(
            f"Para usar efeitos na fala, envie até {MAX_EFFECT_SPEECH_CHARACTERS} caracteres por mensagem."
        )
    return clean_text, result


def performance_hash(*, text: str, score: dict[str, Any] | None = None,
                     segments: list[dict[str, Any]] | None = None) -> str:
    """Bind score/effects to text while preserving legacy plain-text hashes."""
    if score is None and not segments:
        return ""
    payload = {"version": 1, "text": _normalize(text), "score": score, "segments": segments}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
