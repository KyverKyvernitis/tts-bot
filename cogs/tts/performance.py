"""Comandos curtos do TTV e identidade de uma apresentação.

Os efeitos valem para palavras: ``<olá`` entra suavemente, ``olá>`` sai
suavemente e ``olá~`` sustenta a última vogal. ``|`` separa trechos. No
canto, ``~`` separado ocupa a próxima nota com a vogal anterior.

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

        if token == "~" and active:
            if not singing:
                raise ValueError("Na fala, coloque ~ depois da palavra, como: olá~.")
            if not result or boundary_pending:
                raise ValueError("No canto, use ~ depois de uma sílaba para continuar sua vogal.")
            result.append(_segment("~", hold=True))
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
        hold = False
        while right > left and right - 1 in active and token[right - 1] in ">~":
            marker = token[right - 1]
            if marker == ">":
                if fade_out:
                    raise ValueError("Use apenas um > no final da palavra, como: olá>.")
                fade_out = True
            else:
                if hold:
                    raise ValueError("Use apenas um ~ no final da palavra, como: olá~.")
                hold = True
            right -= 1

        if any(left <= offset < right for offset in active):
            raise ValueError("Use < antes da palavra, > ou ~ depois, e | entre palavras.")
        clean_word = token[left:right] + punctuation
        if not clean_word and (fade_in or fade_out or hold or active):
            raise ValueError("O efeito precisa de uma palavra: <olá, olá> ou olá~.")
        if (fade_in or fade_out or hold) and not any(char.isalnum() for char in clean_word):
            raise ValueError("O efeito precisa de uma palavra: <olá, olá> ou olá~.")
        result.append(_segment(clean_word, fade_in=fade_in, fade_out=fade_out, hold=hold))
        has_effects = has_effects or fade_in or fade_out or hold
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
