from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class Mora:
    candidates: tuple[str, ...]
    duration_ms: int = 135
    pause_after_ms: int = 0
    word_end: bool = False
    phrase_end: str = ""
    word_index: int = 0
    mora_index: int = 0
    word_moras: int = 1
    stressed: bool = False
    source_word: str = ""
    deaccented: bool = False
    coda: bool = False
    glide: bool = False


_ROMAJI_TO_KANA = {
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ", "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ", "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
    "bya": "びゃ", "byu": "びゅ", "byo": "びょ", "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
    "mya": "みゃ", "myu": "みゅ", "myo": "みょ", "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ", "va": "ば", "vi": "び", "vu": "ぶ", "ve": "べ", "vo": "ぼ",
    "tsa": "つぁ", "tsi": "つぃ", "tse": "つぇ", "tso": "つぉ", "she": "しぇ", "che": "ちぇ", "je": "じぇ",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
    "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
    "sa": "さ", "shi": "し", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "za": "ざ", "ji": "じ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
    "ta": "た", "chi": "ち", "ti": "ち", "tsu": "つ", "tu": "つ", "te": "て", "to": "と",
    "da": "だ", "di": "でぃ", "du": "どぅ", "de": "で", "do": "ど",
    "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
    "ha": "は", "hi": "ひ", "fu": "ふ", "hu": "ふ", "he": "へ", "ho": "ほ",
    "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
    "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
    "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
    "ya": "や", "yu": "ゆ", "yo": "よ",
    "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wi": "うぃ", "we": "うぇ", "wo": "を",
    "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お", "n": "ん",
}
_ROMAJI_KEYS = sorted(_ROMAJI_TO_KANA, key=len, reverse=True)

# These pauses are deliberately shorter than the old speech-2 defaults. Word
# boundaries are primarily expressed by coarticulation now; punctuation keeps
# a clear hierarchy without adding a tiny stop after every token.
_PUNCT_PAUSES = {",": 85, ";": 135, ":": 115, ".": 180, "!": 155, "?": 180, "\n": 190}
_WORD_GAP_MS = 6
_PT_VOWELS = set("aeiouáéíóúâêôãõàü")
_PT_STRESS_MARKS = set("áéíóúâêôãõ")
_PT_FUNCTION_WORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e",
    "em", "lhe", "lhes", "me", "na", "nas", "no", "nos", "o", "os", "para",
    "por", "que", "se", "sem", "te", "um", "uma", "umas", "uns",
}
_PT_DIPHTHONGS = ("ai", "ei", "oi", "ui", "au", "eu", "ou", "ão", "ãe", "õe")


def _katakana_to_hiragana(text: str) -> str:
    out: list[str] = []
    for char in text:
        code = ord(char)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(char)
    return "".join(out)


def _kana_candidates(kana: str) -> tuple[str, ...]:
    alternates: dict[str, tuple[str, ...]] = {
        "じ": ("じ", "ぢ", "ji"), "ず": ("ず", "づ", "zu"), "し": ("し", "shi", "si"),
        "ち": ("ち", "chi", "ti"), "つ": ("つ", "tsu", "tu"), "ふ": ("ふ", "fu", "hu"),
        "を": ("を", "お", "wo"), "ん": ("ん", "n"),
    }
    return alternates.get(kana, (kana,))


def _split_hiragana_mora(text: str) -> list[str]:
    small = set("ゃゅょぁぃぅぇぉゎ")
    result: list[str] = []
    for char in _katakana_to_hiragana(text):
        if char in small and result:
            result[-1] += char
        elif char == "ー" and result:
            result.append(result[-1][-1])
        elif "ぁ" <= char <= "ゖ" or char == "ん":
            result.append(char)
    return result


def _romaji_to_kana(text: str) -> list[str]:
    value = re.sub(r"[^a-z]", "", text.lower())
    result: list[str] = []
    index = 0
    while index < len(value):
        if index + 1 < len(value) and value[index] == value[index + 1] and value[index] not in "aeioun":
            index += 1
            continue
        matched = False
        for key in _ROMAJI_KEYS:
            if value.startswith(key, index):
                result.append(_ROMAJI_TO_KANA[key])
                index += len(key)
                matched = True
                break
        if not matched:
            char = value[index]
            fallback = {
                "b": "ぶ", "c": "く", "d": "ど", "f": "ふ", "g": "ぐ", "h": "ふ",
                "j": "じ", "k": "く", "l": "る", "m": "む", "p": "ぷ", "q": "く",
                "r": "る", "s": "す", "t": "と", "v": "ぶ", "w": "う", "x": "し", "y": "い", "z": "ず",
            }.get(char)
            if fallback:
                result.append(fallback)
            index += 1
    return result


def _portuguese_word_to_romaji(word: str) -> str:
    # This remains a deliberately small PT-BR -> Japanese-CV approximation, not
    # a full G2P model. Preserve nasal spelling before NFKD removes the tilde,
    # and convert common digraphs before dropping the otherwise silent <h>.
    value = unicodedata.normalize("NFC", word.lower())
    value = (value
             .replace("ão", "aun").replace("ãe", "ain").replace("õe", "oin")
             .replace("ã", "an").replace("õ", "on")
             .replace("ç", "ss"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("nh", "ny").replace("lh", "ry").replace("ch", "sy")
    # Portuguese orthographic h is silent. Strong-r approximations are inserted
    # only after this pass so they keep their intended Japanese h-series sound.
    value = value.replace("h", "")
    replacements = (
        (r"rr", "h"),
        (r"^r(?=[aeiou])", "h"),
        (r"qu(?=[ei])", "k"), (r"gu(?=[ei])", "g"), (r"ph", "f"),
        (r"c(?=[ei])", "s"), (r"g(?=[ei])", "j"), (r"c", "k"), (r"q", "k"),
        (r"(?<=[aeiou])s(?=[aeiou])", "z"), (r"ss", "s"), (r"z$", "s"),
        # Final x is treated like a short fricative coda; elsewhere /sh/ is a
        # safer CV approximation than inserting two independent consonants.
        (r"x$", "s"), (r"x", "sy"), (r"w", "u"),
        # Very common Brazilian palatalization before /i/. This is still a CV
        # approximation: chi/ji are closer than a literal Japanese ti/di.
        (r"ti", "chi"), (r"di", "ji"),
        # PT-BR final m/n nasalizes the preceding vowel; Japanese ん is less
        # intrusive than appending mu/nu. Final l is commonly vocalized /w/.
        (r"[mn]$", "n"), (r"l$", "u"),
        # A final rhotic has no good CV-only equivalent. Dropping it is less
        # disruptive to speech than appending an artificial 'ru' syllable.
        (r"r$", ""),
        # Japanese CV banks usually provide ra/ri/... rather than la/li/....
        (r"l(?=[aeiouy])", "r"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return re.sub(r"[^a-z]", "", value)


def _plain_word(word: str) -> str:
    value = unicodedata.normalize("NFKD", str(word or "").lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch) and ch.isalpha())


def _is_deaccented_word(word: str) -> bool:
    value = unicodedata.normalize("NFC", str(word or "").lower())
    if any(char in _PT_STRESS_MARKS for char in value):
        return False
    return _plain_word(value) in _PT_FUNCTION_WORDS


def _mora_traits(word: str, moras: list[str]) -> tuple[set[int], set[int]]:
    """Return (coda_indexes, glide_indexes) for cheap PT-BR timing hints."""
    if not moras or re.search(r"[ぁ-ゖァ-ヺ]", word):
        return set(), set()
    value = unicodedata.normalize("NFC", str(word or "").lower())
    plain = _plain_word(value)
    codas: set[int] = set()
    glides: set[int] = set()

    last = moras[-1]
    if (plain.endswith(("m", "n")) or any(mark in value for mark in ("ã", "õ"))) and last == "ん":
        codas.add(len(moras) - 1)
    elif plain.endswith(("s", "z", "x")) and last in {"す", "し", "ず", "じ"}:
        codas.add(len(moras) - 1)

    # A vocalized final L is a glide, not a full extra Japanese /u/ syllable.
    if plain.endswith("l") and last == "う":
        glides.add(len(moras) - 1)

    # The overwhelmingly common one-nucleus diphthong case (pai, mãe, não,
    # meu, foi...) maps cleanly onto the first two CV moras. Keep the second
    # element short so it behaves like a glide rather than another syllable.
    groups = _vowel_groups(value)
    if len(groups) == 1 and any(diphthong in value for diphthong in _PT_DIPHTHONGS):
        usable = [index for index in range(len(moras)) if index not in codas]
        if len(usable) >= 2:
            glides.add(usable[1])
    return codas, glides


def _word_to_mora(word: str) -> list[str]:
    if re.search(r"[ぁ-ゖァ-ヺ]", word):
        return _split_hiragana_mora(word)
    ascii_word = unicodedata.normalize("NFKC", word)
    return _romaji_to_kana(_portuguese_word_to_romaji(ascii_word))


def _vowel_groups(word: str) -> list[tuple[int, int]]:
    value = unicodedata.normalize("NFC", str(word or "").lower())
    groups: list[tuple[int, int]] = []
    start: int | None = None
    for index, char in enumerate(value):
        if char in _PT_VOWELS:
            if start is None:
                start = index
        elif start is not None:
            groups.append((start, index))
            start = None
    if start is not None:
        groups.append((start, len(value)))
    return groups


def _portuguese_stress_mora(word: str, mora_count: int) -> int | None:
    """Return an intentionally conservative PT-BR stress approximation.

    This is not a syllabifier. It preserves explicit orthographic stress first,
    then applies the common Portuguese final/penultimate rule and maps that
    vowel nucleus onto the Japanese mora sequence. The deterministic result is
    sufficient for speech timing without adding a heavy linguistic dependency.
    """
    if mora_count <= 0 or re.search(r"[ぁ-ゖァ-ヺ]", word):
        return None
    value = unicodedata.normalize("NFC", str(word or "").lower())
    groups = _vowel_groups(value)
    if not groups:
        return None

    stressed_group: int | None = None
    for group_index, (start, end) in enumerate(groups):
        if any(char in _PT_STRESS_MARKS for char in value[start:end]):
            stressed_group = group_index
            break

    if stressed_group is None:
        plain = unicodedata.normalize("NFKD", value)
        plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
        penultimate_endings = ("a", "e", "o", "as", "es", "os", "am", "em", "ens")
        stressed_group = max(0, len(groups) - 2) if plain.endswith(penultimate_endings) and len(groups) > 1 else len(groups) - 1

    if len(groups) == 1 or mora_count == 1:
        return 0
    mapped = round(stressed_group * (mora_count - 1) / (len(groups) - 1))
    mapped = max(0, min(mora_count - 1, mapped))
    # Orthographic final consonants can create an approximation mora in a CV
    # bank. Keep lexical stress on the preceding vowel-bearing mora.
    plain = unicodedata.normalize("NFKD", value)
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    if mapped == mora_count - 1 and mora_count > 1 and plain and plain[-1] in "mnslzx":
        mapped -= 1
    return mapped


def phonemize(text: str, *, max_moras: int = 240) -> list[Mora]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not normalized:
        return []
    tokens = re.findall(r"[ぁ-ゖァ-ヺーA-Za-zÀ-ÿÇç]+|[,.!?:;\n]", normalized)
    result: list[Mora] = []
    word_index = 0
    for token in tokens:
        if token in _PUNCT_PAUSES:
            if result:
                previous = result[-1]
                result[-1] = replace(
                    previous,
                    pause_after_ms=max(previous.pause_after_ms, _PUNCT_PAUSES[token]),
                    phrase_end=token,
                )
            continue

        moras = _word_to_mora(token)
        deaccented = _is_deaccented_word(token)
        stress_index = None if deaccented else _portuguese_stress_mora(token, len(moras))
        coda_indexes, glide_indexes = _mora_traits(token, moras)
        for mora_index, mora in enumerate(moras):
            coda = mora_index in coda_indexes
            glide = mora_index in glide_indexes
            if coda:
                duration = 82 if mora != "ん" else 92
            elif glide:
                duration = 94
            else:
                duration = 135
            result.append(Mora(
                _kana_candidates(mora),
                duration_ms=duration,
                word_index=word_index,
                mora_index=mora_index,
                word_moras=max(1, len(moras)),
                stressed=stress_index == mora_index,
                source_word=token,
                deaccented=deaccented,
                coda=coda,
                glide=glide,
            ))
            if len(result) >= max(1, int(max_moras)):
                return result
        if moras:
            previous = result[-1]
            result[-1] = replace(
                previous,
                pause_after_ms=max(previous.pause_after_ms, _WORD_GAP_MS),
                word_end=True,
            )
            word_index += 1
    return result
