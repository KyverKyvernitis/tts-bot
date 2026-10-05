from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Keep the intermediate inventory intentionally close to OpenUtau's Portuguese
# G2P symbols. The worker does not embed OpenUtau/ONNX; this lightweight layer
# exists so orthography is resolved to Portuguese sounds *before* we approximate
# those sounds with a Japanese CV voicebank.
VOWELS = frozenset({"a", "a~", "e", "e~", "E", "i", "i~", "o", "o~", "O", "u", "u~"})
GLIDES = frozenset({"j", "w"})
CONSONANTS = frozenset({
    "b", "d", "dZ", "f", "g", "J", "k", "l", "L", "m", "n", "p",
    "r", "R", "s", "S", "t", "tS", "v", "z", "Z",
})

_PT_VOWEL_CHARS = frozenset("aeiouáéíóúâêôãõàü")
_EXPLICIT_STRESS = frozenset("áéíóúâêôãõ")
_DEFAULT_PENULTIMATE_ENDINGS = ("a", "e", "o", "as", "es", "os", "am", "em", "ens")
_ONSET_CLUSTERS = frozenset({
    ("p", "r"), ("b", "r"), ("t", "r"), ("d", "r"), ("k", "r"), ("g", "r"),
    ("f", "r"), ("v", "r"), ("p", "l"), ("b", "l"), ("k", "l"), ("g", "l"),
    ("f", "l"), ("v", "l"),
})


@dataclass(frozen=True, slots=True)
class Phone:
    symbol: str
    nucleus_index: int | None = None
    stressed: bool = False


@dataclass(frozen=True, slots=True)
class Syllable:
    onset: tuple[str, ...]
    vowel: str
    coda: tuple[str, ...]
    stressed: bool = False


@dataclass(frozen=True, slots=True)
class PhoneticWord:
    source: str
    phones: tuple[Phone, ...]
    syllables: tuple[Syllable, ...]

    @property
    def phonemes(self) -> tuple[str, ...]:
        return tuple(phone.symbol for phone in self.phones)


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch) and ch.isalpha())


def _is_vowel_char(char: str) -> bool:
    return char in _PT_VOWEL_CHARS


def _base_vowel(char: str) -> str:
    if char in "aáàâ":
        return "a"
    if char == "ã":
        return "a~"
    if char in "eê":
        return "e"
    if char == "é":
        return "E"
    if char in "ií":
        return "i"
    if char in "oô":
        return "o"
    if char == "ó":
        return "O"
    if char == "õ":
        return "o~"
    if char in "uúü":
        return "u"
    return "a"


def _nasalize(symbol: str) -> str:
    return {
        "a": "a~", "e": "e~", "E": "e~", "i": "i~",
        "o": "o~", "O": "o~", "u": "u~",
    }.get(symbol, symbol)


def _next_letter(value: str, index: int) -> str:
    return value[index + 1] if index + 1 < len(value) else ""


def _previous_letter(value: str, index: int) -> str:
    return value[index - 1] if index > 0 else ""


def _orthographic_nuclei(value: str) -> list[int]:
    """Return approximate written vowel nuclei for stress placement.

    Adjacent vowels are not automatically one nucleus: accented second vowels
    such as ``aí``/``aú`` are hiatuses, while the high-frequency diphthongs
    handled by :func:`_emit_vowel` stay together.
    """
    starts: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        if not _is_vowel_char(char):
            index += 1
            continue
        starts.append(index)
        nxt = value[index + 1] if index + 1 < len(value) else ""
        pair = _plain(char + nxt) if nxt and _is_vowel_char(nxt) else ""
        nasal_pair = char + nxt
        if nxt and nxt not in _EXPLICIT_STRESS and (pair in {"ai", "ei", "oi", "ui", "au", "eu", "iu", "ou"} or nasal_pair in {"ão", "ãe", "õe"}):
            index += 2
        else:
            index += 1
    return starts


def _stress_nucleus(value: str, nucleus_count: int) -> int | None:
    if nucleus_count <= 0:
        return None
    starts = _orthographic_nuclei(value)
    if not starts:
        return None

    explicit_group: int | None = None
    for group_index, start in enumerate(starts):
        end = starts[group_index + 1] if group_index + 1 < len(starts) else len(value)
        # Stop this group's span at the first consonant after it.
        for pos in range(start, end):
            if value[pos] in _EXPLICIT_STRESS:
                explicit_group = group_index
                break
        if explicit_group is not None:
            break

    if explicit_group is None:
        plain = _plain(value)
        if len(starts) > 1 and plain.endswith(_DEFAULT_PENULTIMATE_ENDINGS):
            explicit_group = len(starts) - 2
        else:
            explicit_group = len(starts) - 1

    # Diphthongs collapse two orthographic vowels into one phonetic nucleus, so
    # scale the written group index onto the produced nucleus sequence.
    if len(starts) <= 1 or nucleus_count <= 1:
        return 0
    return max(0, min(nucleus_count - 1, round(explicit_group * (nucleus_count - 1) / (len(starts) - 1))))


def _emit_vowel(value: str, index: int) -> tuple[list[str], int]:
    char = value[index]
    symbol = _base_vowel(char)
    next_char = _next_letter(value, index)
    after_next = value[index + 2] if index + 2 < len(value) else ""

    # Canonical nasal diphthongs first.
    pair = char + next_char
    if pair in {"ão", "Ão", "ÃO"}:
        return ["a~", "w"], 2
    if pair in {"ãe", "Ãe", "ÃE"}:
        return ["a~", "j"], 2
    if pair in {"õe", "Õe", "ÕE"}:
        return ["o~", "j"], 2

    # A vowel followed by coda m/n is nasal in neutral Brazilian Portuguese.
    # Do not consume the n of 'nh'.
    if next_char and next_char in "mn" and not (next_char == "n" and after_next == "h"):
        if not after_next or not _is_vowel_char(after_next):
            return [_nasalize(symbol)], 2

    # Frequent oral diphthongs. Represent the second element as a glide rather
    # than another full nucleus; this is critical when later approximating with
    # a CV bank because the glide can be rendered very short.
    plain_pair = _plain(pair)
    second_is_stressed = next_char in _EXPLICIT_STRESS
    if not second_is_stressed and plain_pair in {"ai", "ei", "oi", "ui"}:
        return [symbol, "j"], 2
    if not second_is_stressed and plain_pair in {"au", "eu", "iu"}:
        return [symbol, "w"], 2
    if not second_is_stressed and plain_pair == "ou":
        # Neutral PT-BR commonly monophthongizes 'ou'. Avoid an extra Japanese
        # vowel unless an explicit pronunciation layer is introduced later.
        return [symbol], 2

    return [symbol], 1


def _scan_phones(word: str) -> list[str]:
    value = unicodedata.normalize("NFC", str(word or "").lower())
    value = re.sub(r"[^a-zàáâãçéêíóôõúü]", "", value)
    phones: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        nxt = _next_letter(value, index)
        nxt2 = value[index + 2] if index + 2 < len(value) else ""
        prev = _previous_letter(value, index)

        if _is_vowel_char(char):
            emitted, consumed = _emit_vowel(value, index)
            phones.extend(emitted)
            index += consumed
            continue

        pair = char + nxt
        if pair == "nh":
            phones.append("J")
            index += 2
            continue
        if pair == "lh":
            phones.append("L")
            index += 2
            continue
        if pair == "ch":
            phones.append("S")
            index += 2
            continue
        if pair == "rr":
            phones.append("R")
            index += 2
            continue
        if pair == "ss":
            phones.append("s")
            index += 2
            continue
        if pair == "qu" and nxt2 and nxt2 in "eiéêí":
            phones.append("k")
            index += 2
            continue
        if pair == "gu" and nxt2 and nxt2 in "eiéêí":
            phones.append("g")
            index += 2
            continue
        if pair in {"sc", "sç", "xc"} and nxt2 and nxt2 in "eiéêí":
            phones.append("s")
            index += 2
            continue

        if char == "b":
            phones.append("b")
        elif char == "ç":
            phones.append("s")
        elif char == "c":
            phones.append("s" if nxt and nxt in "eiéêí" else "k")
        elif char == "d":
            # In much of Brazil /d/ palatalizes before an /i/ realization,
            # including final unstressed -de.
            phones.append("dZ" if (nxt and nxt in "ií") or (nxt == "e" and index + 2 == len(value)) else "d")
        elif char == "f":
            phones.append("f")
        elif char == "g":
            phones.append("Z" if nxt and nxt in "eiéêí" else "g")
        elif char == "h":
            pass
        elif char == "j":
            phones.append("Z")
        elif char in "kq":
            phones.append("k")
        elif char == "l":
            # Coda /l/ in Brazilian Portuguese is commonly vocalized to /w/.
            phones.append("w" if not nxt or not _is_vowel_char(nxt) else "l")
        elif char == "m":
            phones.append("m")
        elif char == "n":
            phones.append("n")
        elif char == "p":
            phones.append("p")
        elif char == "r":
            phones.append("R" if index == 0 or prev in "nls" else "r")
        elif char == "s":
            phones.append("z" if _is_vowel_char(prev) and _is_vowel_char(nxt) else "s")
        elif char == "t":
            phones.append("tS" if (nxt and nxt in "ií") or (nxt == "e" and index + 2 == len(value)) else "t")
        elif char == "v":
            phones.append("v")
        elif char == "w":
            phones.append("w")
        elif char == "x":
            # X is highly lexical in Portuguese. These two high-confidence
            # contexts cover exame/exato and word-initial x; other cases use /S/
            # rather than pretending a certainty the lightweight G2P does not have.
            if index == 1 and value.startswith("e") and _is_vowel_char(nxt):
                phones.append("z")
            else:
                phones.append("S")
        elif char == "y":
            phones.append("j")
        elif char == "z":
            phones.append("s" if not nxt else "z")
        index += 1

    return phones


def _reduce_final_unstressed(phones: list[str], stressed_nucleus: int | None) -> list[str]:
    nucleus_positions = [index for index, symbol in enumerate(phones) if symbol in VOWELS]
    if not nucleus_positions:
        return phones
    final_pos = nucleus_positions[-1]
    final_nucleus = len(nucleus_positions) - 1
    if final_nucleus == stressed_nucleus:
        return phones
    output = list(phones)
    if output[final_pos] == "e":
        output[final_pos] = "i"
    elif output[final_pos] == "o":
        output[final_pos] = "u"
    return output


def _syllabify(symbols: list[str], stressed_nucleus: int | None) -> tuple[Syllable, ...]:
    nucleus_positions = [index for index, symbol in enumerate(symbols) if symbol in VOWELS]
    if not nucleus_positions:
        return ()

    syllables: list[Syllable] = []
    previous_nucleus = -1
    for nucleus_number, nucleus_pos in enumerate(nucleus_positions):
        between = symbols[previous_nucleus + 1:nucleus_pos]
        if nucleus_number == 0:
            onset = tuple(between)
            previous_coda: tuple[str, ...] = ()
        else:
            # Reassign consonants between two nuclei: the largest legal PT onset
            # goes forward, everything else closes the previous syllable.
            if len(between) >= 2 and tuple(between[-2:]) in _ONSET_CLUSTERS:
                split = len(between) - 2
            elif between:
                split = len(between) - 1
            else:
                split = 0
            previous_coda = tuple(between[:split])
            onset = tuple(between[split:])
            if previous_coda:
                prev = syllables[-1]
                syllables[-1] = Syllable(prev.onset, prev.vowel, prev.coda + previous_coda, prev.stressed)

        next_nucleus = nucleus_positions[nucleus_number + 1] if nucleus_number + 1 < len(nucleus_positions) else len(symbols)
        # Glides directly following the vowel belong to this syllable. Other
        # consonants are temporarily left for the next iteration/coda split.
        coda: list[str] = []
        cursor = nucleus_pos + 1
        while cursor < next_nucleus and symbols[cursor] in GLIDES:
            coda.append(symbols[cursor])
            cursor += 1
        syllables.append(Syllable(
            onset=onset,
            vowel=symbols[nucleus_pos],
            coda=tuple(coda),
            stressed=nucleus_number == stressed_nucleus,
        ))
        previous_nucleus = nucleus_pos + len(coda)

    # Remaining phones after the last nucleus are the final coda. Because
    # previous_nucleus may include diphthong glides, avoid duplicating them.
    last_nucleus = nucleus_positions[-1]
    trailing = symbols[last_nucleus + 1:]
    glide_prefix = 0
    while glide_prefix < len(trailing) and trailing[glide_prefix] in GLIDES:
        glide_prefix += 1
    remaining = tuple(trailing[glide_prefix:])
    if remaining:
        prev = syllables[-1]
        syllables[-1] = Syllable(prev.onset, prev.vowel, prev.coda + remaining, prev.stressed)
    return tuple(syllables)


def g2p_word(word: str, *, deaccented: bool = False) -> PhoneticWord:
    source = unicodedata.normalize("NFC", str(word or "")).strip()
    symbols = _scan_phones(source)
    nucleus_count = sum(symbol in VOWELS for symbol in symbols)
    stressed_nucleus = None if deaccented else _stress_nucleus(source.lower(), nucleus_count)
    symbols = _reduce_final_unstressed(symbols, stressed_nucleus)

    phones: list[Phone] = []
    nucleus = -1
    for symbol in symbols:
        if symbol in VOWELS:
            nucleus += 1
            phones.append(Phone(symbol, nucleus_index=nucleus, stressed=nucleus == stressed_nucleus))
        else:
            phones.append(Phone(symbol, nucleus_index=None, stressed=False))
    syllables = _syllabify([phone.symbol for phone in phones], stressed_nucleus)
    return PhoneticWord(source=source, phones=tuple(phones), syllables=syllables)
