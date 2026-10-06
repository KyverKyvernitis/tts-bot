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
    coverage: str = "standard-cv"


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
    coverage: str = "standard-cv",
) -> Mora:
    return Mora(
        candidates=_prefer_available(candidates, resolve_alias),
        duration_ms=duration_ms,
        stressed=stressed,
        deaccented=deaccented,
        role=role,
        source_phonemes=source,
        coverage=coverage,
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



# Teto English 150401 uses a Delta/X-SAMPA-style CVVC inventory.  Keep PT-BR
# G2P independent from the bank: this layer only maps canonical PT phones onto
# X-SAMPA units that are actually present in the loaded oto.ini.
_XSAMPA_VOWELS = {
    "a": "a", "a~": "a", "e": "e", "e~": "e", "E": "E",
    "i": "i", "i~": "i", "o": "o", "o~": "o", "O": "O",
    "u": "u", "u~": "u",
}
_XSAMPA_DIPHTHONGS = {
    ("a", "j"): "aI", ("e", "j"): "eI", ("E", "j"): "eI",
    ("o", "j"): "OI", ("O", "j"): "OI",
    ("a", "w"): "aU", ("o", "w"): "oU", ("O", "w"): "oU",
}
_ENGLISH_ROLE_DURATIONS = {
    "transition": 58,
    "cluster": 54,
    "coda": 58,
    "glide": 54,
    "nasal": 66,
}


def _xsampa_phone_sequence(phone: str) -> tuple[str, ...]:
    # PT /nh/, /lh/ and strong /r/ do not exist as dedicated symbols in the
    # official English bank.  Represent them with the closest *composable*
    # inventory rather than inserting Japanese vowels.
    if phone == "J":       # /ɲ/ -> n+j
        return ("n", "j")
    if phone == "L":       # /ʎ/ -> l+j
        return ("l", "j")
    if phone == "R":       # Brazilian strong r is closer to English /h/
        return ("h",)
    if phone == "r":
        # The Teto bank has rich r-context aliases but no isolated r.  Keep r
        # as the stable default; candidate generation also probes tap /4/.
        return ("r",)
    return (phone,)


def _xsampa_onset(phones: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for phone in phones:
        output.extend(_xsampa_phone_sequence(phone))
    return tuple(output)


def _xsampa_vowel(phone: str) -> str:
    return _XSAMPA_VOWELS.get(phone, phone.rstrip("~") or "@")


def _english_resolve(
    tiers: Iterable[tuple[str, Iterable[str]]],
    resolve_alias: Callable[[Iterable[str]], object | None] | None,
) -> tuple[tuple[str, ...], str]:
    """Pick the first alias tier that the actual English oto.ini can render.

    The full tier ordering is retained behind the chosen alias so renderer-side
    resolution remains deterministic even if the index normalizes the spelling.
    """
    first: tuple[str, ...] = ()
    first_kind = "approximation"
    for kind, values in tiers:
        candidates = _unique(values)
        if not candidates:
            continue
        if not first:
            first, first_kind = candidates, kind
        if resolve_alias is None:
            return candidates, kind
        try:
            entry = resolve_alias(candidates)
        except Exception:
            entry = None
        if entry is not None:
            alias = str(getattr(entry, "alias", "") or "").strip()
            return _unique((alias, *candidates)), kind
    return first, first_kind


def _english_mora(
    tiers: Iterable[tuple[str, Iterable[str]]], *, duration_ms: int, role: str,
    source: tuple[str, ...], stressed: bool = False, deaccented: bool = False,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> Mora:
    candidates, coverage = _english_resolve(tiers, resolve_alias)
    return Mora(
        candidates=candidates,
        duration_ms=duration_ms,
        stressed=stressed,
        deaccented=deaccented,
        role=role,
        source_phonemes=source,
        coverage=coverage,
    )


def _english_nucleus_candidates(
    *, onset: tuple[str, ...], vowel: str, previous_vowel: str, word_start: bool,
) -> list[tuple[str, tuple[str, ...]]]:
    cluster = "".join(onset)
    last = onset[-1] if onset else ""
    tiers: list[tuple[str, tuple[str, ...]]] = []
    if word_start:
        if cluster:
            tiers.append(("cvvc-direct", (f"- {cluster}{vowel}",)))
            if last == "r":
                tiers.append(("cvvc-direct", (f"- {cluster[:-1]}4{vowel}",)))
        else:
            tiers.append(("cvvc-direct", (f"- {vowel}",)))
    elif previous_vowel:
        if cluster:
            tiers.append(("cvvc-direct", (
                f"{previous_vowel} {cluster}{vowel}",
                f"{previous_vowel}{cluster} {vowel}",
            )))
        else:
            tiers.append(("cvvc-direct", (f"{previous_vowel} {vowel}", f"{previous_vowel}{vowel}")))
    if cluster:
        base = [f"{cluster}{vowel}", f"{last}{vowel}", f"{last} {vowel}"]
        if last == "r":
            base.extend((f"4{vowel}", f"4 {vowel}"))
        tiers.append(("cvvc-direct", tuple(base)))
    else:
        tiers.append(("cvvc-direct", (vowel,)))
    return tiers


def _english_cluster_units(
    onset: tuple[str, ...], *, word_start: bool, previous_vowel: str,
    resolve_alias: Callable[[Iterable[str]], object | None] | None,
) -> list[Mora]:
    if not onset:
        return []
    units: list[Mora] = []
    cluster = "".join(onset)
    first = onset[0]
    if word_start and len(onset) > 1:
        units.append(_english_mora(
            [
                ("cluster-hit", (f"- {cluster}",)),
                ("cvvc-transition", (f"- {first}", first)),
            ],
            duration_ms=_ENGLISH_ROLE_DURATIONS["cluster"], role="cluster",
            source=tuple(onset), deaccented=True, resolve_alias=resolve_alias,
        ))
    elif word_start:
        units.append(_english_mora(
            [("cvvc-transition", (f"- {first}", first))],
            duration_ms=_ENGLISH_ROLE_DURATIONS["transition"], role="transition",
            source=(first,), deaccented=True, resolve_alias=resolve_alias,
        ))
    elif previous_vowel:
        # A VC/VCC transition retains the previous vowel's exit into the onset.
        prefix2 = "".join(onset[:2])
        units.append(_english_mora(
            [
                ("cluster-hit", (f"{previous_vowel} {prefix2}",)) if len(onset) > 1 else ("cvvc-transition", ()),
                ("cvvc-transition", (f"{previous_vowel} {first}", f"{previous_vowel}{first}")),
            ],
            duration_ms=_ENGLISH_ROLE_DURATIONS["transition"], role="transition",
            source=(previous_vowel, first), deaccented=True, resolve_alias=resolve_alias,
        ))

    cluster_already_covered = False
    if units and units[0].coverage == "cluster-hit":
        compact = units[0].candidates[0].replace("-", "").replace(" ", "") if units[0].candidates else ""
        cluster_already_covered = cluster in compact

    if len(onset) > 1 and not cluster_already_covered:
        # Each CC alias bridges the cluster without an epenthetic vowel.
        for left, right in zip(onset, onset[1:]):
            units.append(_english_mora(
                [
                    ("cluster-hit", (f"{left} {right}", f"{left}{right}")),
                    ("approximation", (left, right)),
                ],
                duration_ms=_ENGLISH_ROLE_DURATIONS["cluster"], role="cluster",
                source=(left, right), deaccented=True, resolve_alias=resolve_alias,
            ))
    return units


def _english_coda_units(
    *, vowel: str, coda: tuple[str, ...], nasal: bool,
    resolve_alias: Callable[[Iterable[str]], object | None] | None,
) -> list[Mora]:
    units: list[Mora] = []
    previous = vowel
    phones: list[str] = []
    if nasal:
        phones.append("N")
    for phone in coda:
        phones.extend(_xsampa_phone_sequence(phone))
    for raw in phones:
        phone = "4" if raw == "r" else raw
        role = "nasal" if raw == "N" else "glide" if raw in {"j", "w"} else "coda"
        units.append(_english_mora(
            [
                ("cvvc-transition", (f"{previous} {phone}", f"{previous}{phone}")),
                ("approximation", (phone, raw)),
            ],
            duration_ms=_ENGLISH_ROLE_DURATIONS[role], role=role,
            source=(raw,), deaccented=True, resolve_alias=resolve_alias,
        ))
        previous = phone
    return units


def _english_word_to_moras(
    word: PhoneticWord, *, deaccented: bool,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> list[Mora]:
    moras: list[Mora] = []
    previous_vowel = ""
    for syllable_index, syllable in enumerate(word.syllables):
        onset = _xsampa_onset(syllable.onset)
        base_vowel = _xsampa_vowel(syllable.vowel)
        coda_source = list(syllable.coda)
        diphthong = None
        if coda_source and coda_source[0] in {"j", "w"}:
            diphthong = _XSAMPA_DIPHTHONGS.get((base_vowel, coda_source[0]))
        vowel = diphthong or base_vowel
        if diphthong:
            coda_source = coda_source[1:]
        word_start = syllable_index == 0

        nucleus_tiers = _english_nucleus_candidates(
            onset=onset, vowel=vowel, previous_vowel=previous_vowel, word_start=word_start,
        )
        nucleus_candidates, nucleus_coverage = _english_resolve(nucleus_tiers, resolve_alias)

        # If a complete start/VCV/CCV alias exists it already contains the onset
        # transition; otherwise explicitly schedule CVVC transition/CC units.
        full_alias = nucleus_candidates[0] if nucleus_candidates else ""
        cluster_encoded = bool(onset) and (
            (word_start and full_alias.startswith("- ") and "".join(onset) in full_alias)
            or (not word_start and previous_vowel and "".join(onset) in full_alias and " " in full_alias)
            or (len(onset) > 1 and full_alias.startswith("".join(onset)))
        )
        if onset and not cluster_encoded:
            moras.extend(_english_cluster_units(
                onset, word_start=word_start, previous_vowel=previous_vowel,
                resolve_alias=resolve_alias,
            ))

        moras.append(Mora(
            candidates=nucleus_candidates,
            duration_ms=138,
            stressed=bool(syllable.stressed and not deaccented),
            deaccented=deaccented,
            role="nucleus",
            source_phonemes=tuple(syllable.onset) + (syllable.vowel,) + (tuple(syllable.coda[:1]) if diphthong else ()),
            coverage=nucleus_coverage,
        ))

        nasal = syllable.vowel.endswith("~")
        moras.extend(_english_coda_units(
            vowel=vowel, coda=tuple(coda_source), nasal=nasal, resolve_alias=resolve_alias,
        ))
        # The bank's next VCV transition should reference the lexical vowel, not
        # a short coda consonant; this mirrors OpenUtau's prevV handling.
        previous_vowel = vowel
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
    voicebank_profile: str = "standard",
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
            phonetic = g2p_word(token, deaccented=deaccented)
            if str(voicebank_profile).lower().startswith("english"):
                word_moras = _english_word_to_moras(
                    phonetic, deaccented=deaccented, resolve_alias=resolve_alias
                )
            else:
                word_moras = _phonetic_word_to_moras(
                    phonetic, deaccented=deaccented, resolve_alias=resolve_alias
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
