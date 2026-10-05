from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .ptbr_g2p import PhoneticWord, g2p_word


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
    deaccented: bool = False
    role: str = "nucleus"  # nucleus | epenthetic | coda | glide | nasal
    source_phonemes: tuple[str, ...] = ()


# This mapping is only the final PT-phone -> Japanese-CV approximation layer.
# Portuguese orthography is resolved in ptbr_g2p.py first; keeping the layers
# separate is the main architectural change in speech-4.
_ROMAJI_TO_KANA = {
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ", "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ", "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
    "bya": "びゃ", "byu": "びゅ", "byo": "びょ", "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
    "mya": "みゃ", "myu": "みゅ", "myo": "みょ", "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
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

_PUNCT_PAUSES = {",": 85, ";": 135, ":": 115, ".": 180, "!": 155, "?": 180, "\n": 190}
_WORD_GAP_MS = 6
_PT_STRESS_MARKS = set("áéíóúâêôãõ")
_PT_FUNCTION_WORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e",
    "em", "lhe", "lhes", "me", "na", "nas", "no", "nos", "o", "os", "para",
    "por", "que", "se", "sem", "te", "um", "uma", "umas", "uns",
}

# Short auxiliary units are deliberately much shorter than lexical nuclei. They
# hide the vowel that a Japanese CV bank must add to realize a Portuguese cluster
# or final consonant (e.g. the first /b/ in "Brasil").
_ROLE_DURATIONS = {
    "epenthetic": 48,
    "coda": 52,
    "glide": 50,
    "nasal": 66,
}


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = unicodedata.normalize("NFKC", str(value or "")).strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return tuple(result)


def _katakana_to_hiragana(text: str) -> str:
    out: list[str] = []
    for char in text:
        code = ord(char)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(char)
    return "".join(out)


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


def _kana_aliases(kana: str, *extra: str) -> tuple[str, ...]:
    alternates: dict[str, tuple[str, ...]] = {
        "じ": ("じ", "ぢ", "ji"), "ず": ("ず", "づ", "zu"), "し": ("し", "shi", "si"),
        "ち": ("ち", "chi", "ti"), "つ": ("つ", "tsu", "tu"), "ふ": ("ふ", "fu", "hu"),
        "を": ("を", "お", "wo"), "ん": ("ん", "n"),
    }
    return _unique((*alternates.get(kana, (kana,)), *extra))


def _plain_word(word: str) -> str:
    value = unicodedata.normalize("NFKD", str(word or "").lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch) and ch.isalpha())


def _is_deaccented_word(word: str) -> bool:
    value = unicodedata.normalize("NFC", str(word or "").lower())
    if any(char in _PT_STRESS_MARKS for char in value):
        return False
    return _plain_word(value) in _PT_FUNCTION_WORDS


def _base_vowel(phone: str) -> str:
    return {
        "a~": "a", "e~": "e", "i~": "i", "o~": "o", "u~": "u",
        "E": "e", "O": "o",
    }.get(phone, phone)


def _cv_aliases(consonant: str, vowel_phone: str) -> tuple[str, ...]:
    vowel = _base_vowel(vowel_phone)
    if vowel not in {"a", "e", "i", "o", "u"}:
        vowel = "a"

    if not consonant:
        kana = _ROMAJI_TO_KANA[vowel]
        return _kana_aliases(kana, vowel)

    # Preserve the stop in PT /tu, du/. A plain Japanese CV bank normally lacks
    # dedicated [tu]/[du], so prefer optional small-vowel aliases when present
    # and fall back to to/do before accepting tsu-like substitutions.
    if consonant == "t" and vowel == "u":
        return _unique(("とぅ", "tu", "と", "to", "つ", "tsu"))
    if consonant == "d" and vowel == "u":
        return _unique(("どぅ", "du", "ど", "do"))
    if consonant == "s" and vowel == "i":
        return _unique(("すぃ", "si", "し", "shi"))
    if consonant == "z" and vowel == "i":
        return _unique(("ずぃ", "zi", "じ", "ji"))

    # PT phone -> Japanese approximation. Special cases list a closer optional
    # alias before the guaranteed plain-Japanese fallback so the actual oto.ini
    # decides what the installed voicebank can support.
    if consonant == "v":
        voiced_v = {"a": "ゔぁ", "e": "ゔぇ", "i": "ゔぃ", "o": "ゔぉ", "u": "ゔ"}[vowel]
        fallback = _ROMAJI_TO_KANA[f"b{vowel}"]
        return _unique((voiced_v, voiced_v.replace("ゔ", "ヴ"), f"v{vowel}", fallback, f"b{vowel}"))
    if consonant == "L":
        close = {"a": "りゃ", "e": "りぇ", "i": "り", "o": "りょ", "u": "りゅ"}[vowel]
        fallback = _ROMAJI_TO_KANA[f"r{vowel}"]
        return _unique((close, f"ry{vowel}", fallback, f"r{vowel}"))
    if consonant == "J":
        close = {"a": "にゃ", "e": "にぇ", "i": "に", "o": "にょ", "u": "にゅ"}[vowel]
        fallback = _ROMAJI_TO_KANA[f"n{vowel}"]
        return _unique((close, f"ny{vowel}", fallback, f"n{vowel}"))

    prefix = {
        "b": "b", "d": "d", "dZ": "j", "f": "f", "g": "g", "j": "y",
        "k": "k", "l": "r", "m": "m", "n": "n", "p": "p", "r": "r",
        "R": "h", "s": "s", "S": "sh", "t": "t", "tS": "ch", "w": "w",
        "z": "z", "Z": "j",
    }.get(consonant, "")
    key = f"{prefix}{vowel}"
    kana = _ROMAJI_TO_KANA.get(key)
    if kana is None:
        # Not every glide/palatal sequence exists in a plain CV bank. Fall back
        # to the vowel rather than manufacturing an unrelated full syllable.
        return _kana_aliases(_ROMAJI_TO_KANA[vowel], vowel)

    fallbacks: list[str] = [key]
    if consonant == "S":
        plain = _ROMAJI_TO_KANA.get(f"s{vowel}")
        if plain:
            fallbacks.extend((plain, f"s{vowel}"))
    elif consonant in {"Z", "dZ"}:
        plain = _ROMAJI_TO_KANA.get(f"z{vowel}") or _ROMAJI_TO_KANA.get(f"d{vowel}")
        if plain:
            fallbacks.append(plain)
    elif consonant == "R":
        plain = _ROMAJI_TO_KANA.get(f"r{vowel}")
        if plain:
            fallbacks.extend((plain, f"r{vowel}"))
    return _unique((kana, *fallbacks))


def _auxiliary_aliases(phone: str, *, role: str) -> tuple[str, ...]:
    if phone in {"m", "n"} or role == "nasal":
        return _kana_aliases("ん", "n")
    if phone == "j":
        return _kana_aliases("い", "i")
    if phone in {"w", "l"}:
        return _kana_aliases("う", "u")
    if phone in {"s", "S"}:
        return _kana_aliases("す" if phone == "s" else "し", "su" if phone == "s" else "shi")
    if phone in {"z", "Z"}:
        return _kana_aliases("ず" if phone == "z" else "じ", "zu" if phone == "z" else "ji")
    if phone in {"r", "R"}:
        return _kana_aliases("る", "ru")

    epenthetic_vowel = {
        "b": "u", "d": "o", "dZ": "i", "f": "u", "g": "u", "J": "u",
        "k": "u", "L": "u", "p": "u", "t": "o", "tS": "i", "v": "u",
    }.get(phone, "u")
    return _cv_aliases(phone, epenthetic_vowel)


def _prefer_available(
    candidates: tuple[str, ...],
    resolve_alias: Callable[[Iterable[str]], object | None] | None,
) -> tuple[str, ...]:
    if resolve_alias is None:
        return candidates
    try:
        entry = resolve_alias(candidates)
    except Exception:
        return candidates
    alias = str(getattr(entry, "alias", "") or "").strip() if entry is not None else ""
    if not alias:
        return candidates
    return _unique((alias, *candidates))


def _make_mora(
    candidates: tuple[str, ...], *, duration_ms: int, role: str, source: tuple[str, ...],
    stressed: bool = False, deaccented: bool = False,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> Mora:
    return Mora(
        candidates=_prefer_available(candidates, resolve_alias),
        duration_ms=duration_ms,
        stressed=stressed,
        deaccented=deaccented,
        role=role,
        source_phonemes=source,
    )


def _phonetic_word_to_moras(
    word: PhoneticWord, *, deaccented: bool,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> list[Mora]:
    moras: list[Mora] = []
    for syllable in word.syllables:
        onset = list(syllable.onset)
        # Japanese CV cannot express a PT consonant cluster directly. Realize
        # all but the final onset consonant as tiny auxiliary units, then place
        # the lexical vowel on the final onset consonant. This preserves the
        # cluster's consonantal cues without a full Japanese epenthetic vowel.
        for phone in onset[:-1]:
            moras.append(_make_mora(
                _auxiliary_aliases(phone, role="epenthetic"),
                duration_ms=_ROLE_DURATIONS["epenthetic"],
                role="epenthetic",
                source=(phone,),
                deaccented=True,
                resolve_alias=resolve_alias,
            ))

        last_onset = onset[-1] if onset else ""
        moras.append(_make_mora(
            _cv_aliases(last_onset, syllable.vowel),
            duration_ms=135,
            role="nucleus",
            source=tuple((*onset[-1:], syllable.vowel)),
            stressed=bool(syllable.stressed and not deaccented),
            deaccented=deaccented,
            resolve_alias=resolve_alias,
        ))

        if syllable.vowel.endswith("~"):
            moras.append(_make_mora(
                _auxiliary_aliases("n", role="nasal"),
                duration_ms=_ROLE_DURATIONS["nasal"],
                role="nasal",
                source=(syllable.vowel,),
                deaccented=True,
                resolve_alias=resolve_alias,
            ))

        for phone in syllable.coda:
            role = "glide" if phone in {"j", "w"} else "coda"
            moras.append(_make_mora(
                _auxiliary_aliases(phone, role=role),
                duration_ms=_ROLE_DURATIONS[role],
                role=role,
                source=(phone,),
                deaccented=True,
                resolve_alias=resolve_alias,
            ))
    return moras


def _kana_word_to_moras(
    word: str, *, deaccented: bool,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> list[Mora]:
    output: list[Mora] = []
    raw = _split_hiragana_mora(word)
    for index, mora in enumerate(raw):
        output.append(_make_mora(
            _kana_aliases(mora),
            duration_ms=150 if mora == "ん" else 135,
            role="nasal" if mora == "ん" else "nucleus",
            source=(mora,),
            stressed=(index == 0 and not deaccented),
            deaccented=deaccented,
            resolve_alias=resolve_alias,
        ))
    return output


def phonemize(
    text: str,
    *,
    max_moras: int = 240,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> list[Mora]:
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

        deaccented = _is_deaccented_word(token)
        if re.search(r"[ぁ-ゖァ-ヺ]", token):
            word_moras = _kana_word_to_moras(token, deaccented=deaccented, resolve_alias=resolve_alias)
        else:
            word_moras = _phonetic_word_to_moras(
                g2p_word(token, deaccented=deaccented), deaccented=deaccented, resolve_alias=resolve_alias
            )

        total = max(1, len(word_moras))
        for mora_index, mora in enumerate(word_moras):
            result.append(replace(
                mora,
                word_index=word_index,
                mora_index=mora_index,
                word_moras=total,
            ))
            if len(result) >= max(1, int(max_moras)):
                return result
        if word_moras:
            previous = result[-1]
            result[-1] = replace(
                previous,
                pause_after_ms=max(previous.pause_after_ms, _WORD_GAP_MS),
                word_end=True,
            )
            word_index += 1
    return result


__all__ = ["Mora", "phonemize", "g2p_word"]
