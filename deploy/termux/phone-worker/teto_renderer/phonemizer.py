from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .ptbr_g2p import PhoneticWord, g2p_word


VOICEPEAK_READING_VERSION = "ptbr-kana-v1"


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
    profile: str = "standard"
    planner_cost: float = 0.0
    syllable_index: int = 0
    word_syllables: int = 1


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


def _xsampa_onset(phones: Iterable[str], *, dedicated_palatal: bool = False) -> tuple[str, ...]:
    output: list[str] = []
    for phone in phones:
        if dedicated_palatal and phone in {"J", "L"}:
            output.append(phone)
        else:
            output.extend(_xsampa_phone_sequence(phone))
    return tuple(output)


def _xsampa_vowel(phone: str) -> str:
    return _XSAMPA_VOWELS.get(phone, phone.rstrip("~") or "@")


def _english_alias_score(kind: str, alias: str, entry: object | None, rank: int) -> float:
    """Cost a renderable English alias by context coverage and OTO health.

    The 4B planner was greedy: the first spelling that existed won.  The English
    bank has many aliases for the same phonetic boundary, so that rule often
    selected a weak CV fallback even when a full-context VCV/cluster recording
    was available later in the tier.  Keep coverage quality dominant, then use
    the actual oto.ini timing as a small tie-breaker.
    """
    base = {
        "cvvc-direct": 0.0,
        "cluster-hit": 8.0,
        "cvvc-transition": 14.0,
        "approximation": 42.0,
    }.get(str(kind), 48.0)
    text = str(alias or "").strip()
    score = base + max(0, int(rank)) * 1.5
    if text.startswith("- "):
        score -= 2.5
    if " " in text:
        score -= 2.0
    if len(text.replace("-", "").replace(" ", "")) >= 3:
        score -= 1.0
    if entry is not None:
        try:
            pre = max(0.0, float(getattr(entry, "preutterance_ms", 0.0) or 0.0))
            overlap = max(0.0, float(getattr(entry, "overlap_ms", 0.0) or 0.0))
            consonant = max(0.0, float(getattr(entry, "consonant_ms", 0.0) or 0.0))
        except (TypeError, ValueError):
            pre = overlap = consonant = 0.0
        contextual = text.startswith("- ") or " " in text
        if contextual and pre <= 0.0:
            score += 5.0
        if pre > 0.0 and overlap <= 0.0:
            score += 2.0
        if 4.0 <= overlap <= pre + 8.0:
            score -= 1.5
        if consonant > 0.0:
            score -= 0.5
        if pre > 220.0:
            score += min(12.0, (pre - 220.0) / 25.0)
    return round(max(0.0, score), 3)


def _english_resolve(
    tiers: Iterable[tuple[str, Iterable[str]]],
    resolve_alias: Callable[[Iterable[str]], object | None] | None,
) -> tuple[tuple[str, ...], str, float]:
    """Select the lowest-cost renderable alias instead of the first hit."""
    first: tuple[str, ...] = ()
    first_kind = "approximation"
    if resolve_alias is None:
        for kind, values in tiers:
            candidates = _unique(values)
            if candidates:
                return candidates, kind, _english_alias_score(kind, candidates[0], None, 0)
        return first, first_kind, 99.0

    renderable: list[tuple[float, int, str, str, object]] = []
    flattened: list[str] = []
    order = 0
    for kind, values in tiers:
        candidates = _unique(values)
        if not candidates:
            continue
        if not first:
            first, first_kind = candidates, kind
        for rank, candidate in enumerate(candidates):
            flattened.append(candidate)
            try:
                entry = resolve_alias((candidate,))
            except Exception:
                entry = None
            if entry is None:
                order += 1
                continue
            alias = str(getattr(entry, "alias", "") or candidate).strip() or candidate
            score = _english_alias_score(kind, alias, entry, rank)
            renderable.append((score, order, kind, alias, entry))
            order += 1

    if not renderable:
        return first, "approximation", 99.0
    score, _, kind, alias, _ = min(renderable, key=lambda item: (item[0], item[1]))
    return _unique((alias, *flattened)), kind, score


def _english_mora(
    tiers: Iterable[tuple[str, Iterable[str]]], *, duration_ms: int, role: str,
    source: tuple[str, ...], stressed: bool = False, deaccented: bool = False,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> Mora:
    candidates, coverage, planner_cost = _english_resolve(tiers, resolve_alias)
    return Mora(
        candidates=candidates,
        duration_ms=duration_ms,
        stressed=stressed,
        deaccented=deaccented,
        role=role,
        source_phonemes=source,
        coverage=coverage,
        profile="english-cvvc",
        planner_cost=planner_cost,
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
    compositional_palatal_cv: bool = False,
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
        fallback_clusters = (f"- {cluster}", cluster) if len(onset) > 1 else ()
        try:
            full_fallback = bool(
                fallback_clusters and resolve_alias is not None and resolve_alias(fallback_clusters) is not None
            )
        except Exception:
            full_fallback = False
        # If the vowel-to-cluster transition is absent, a recorded full attack
        # retains the cluster without scheduling a missing VC plus another CC.
        # Do not let an isolated first consonant outrank an available cluster.
        fallback = fallback_clusters if full_fallback else (*fallback_clusters, f"- {first}", first)
        units.append(_english_mora(
            [
                ("cluster-hit", (f"{previous_vowel} {prefix2}",)) if len(onset) > 1 else ("cvvc-transition", ()),
                ("cvvc-transition", (f"{previous_vowel} {first}", f"{previous_vowel}{first}")),
                ("approximation", fallback),
            ],
            duration_ms=_ENGLISH_ROLE_DURATIONS["transition"], role="transition",
            source=(previous_vowel, first), deaccented=True, resolve_alias=resolve_alias,
        ))

    cluster_already_covered = False
    if units and units[0].candidates:
        alias = units[0].candidates[0]
        compact = alias.replace("-", "").replace(" ", "")
        try:
            renderable = resolve_alias is None or resolve_alias((alias,)) is not None
        except Exception:
            renderable = False
        cluster_already_covered = bool(renderable and cluster in compact)
        if cluster_already_covered and units[0].coverage == "approximation":
            units[0] = replace(units[0], role="cluster", source_phonemes=tuple(onset))

    if len(onset) > 1 and not cluster_already_covered:
        # Each CC alias bridges the cluster without an epenthetic vowel.
        for left, right in zip(onset, onset[1:]):
            cc_aliases = (f"{left} {right}", f"{left}{right}")
            if (
                compositional_palatal_cv and not word_start and previous_vowel
                and onset in {("n", "j"), ("l", "j")}
                and units and units[0].coverage == "cvvc-transition"
                and resolve_alias is not None
            ):
                try:
                    vc_renderable = resolve_alias((units[0].candidates[0],)) is not None
                    cc_renderable = resolve_alias(cc_aliases) is not None
                except Exception:
                    vc_renderable = cc_renderable = False
                if vc_renderable and not cc_renderable:
                    # For /nh/ -> n+j and /lh/ -> l+j, a real VC has already
                    # articulated n/l and the selected jV owns j. Repeating an
                    # isolated n/l for a missing CC would duplicate its attack.
                    continue
            units.append(_english_mora(
                [
                    ("cluster-hit", cc_aliases),
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
        contextual = [f"{previous} {phone}", f"{previous}{phone}"]
        if raw == "r":
            # Teto English has vowel+r exits, but no isolated r/4. A bank
            # without tap recordings must retain this contextual rhotic.
            contextual.extend((f"{previous} {raw}", f"{previous}{raw}"))
        fallback = [phone, raw]
        if previous == "N" and role == "glide" and resolve_alias is not None:
            try:
                standalone = resolve_alias(_unique(fallback))
            except Exception:
                standalone = None
            if standalone is None:
                # A nasal+glide recording is ideal; an isolated glide also
                # avoids reintroducing an oral vowel after the nasal tail.
                # Only banks without either need the original vowel's glide
                # exit as a contextual approximation. Keep N as its own unit.
                fallback.extend((f"{vowel} {phone}", f"{vowel}{phone}"))
        units.append(_english_mora(
            [
                ("cvvc-transition", tuple(contextual)),
                ("approximation", tuple(fallback)),
            ],
            duration_ms=_ENGLISH_ROLE_DURATIONS[role], role=role,
            source=(raw,), deaccented=True, resolve_alias=resolve_alias,
        ))
        previous = phone
    return units


def _english_nucleus_duration(
    syllable, *, deaccented: bool, syllable_index: int, word_syllables: int, diphthong: bool
) -> int:
    """Speech timing seed for a lexical vowel, before rate/prosody scaling."""
    duration = 108.0
    if deaccented:
        duration -= 16.0
    elif bool(getattr(syllable, "stressed", False)):
        duration += 14.0
    onset = tuple(getattr(syllable, "onset", ()) or ())
    coda = tuple(getattr(syllable, "coda", ()) or ())
    if len(onset) >= 2:
        duration += 4.0
    if coda:
        duration += 4.0
    if diphthong:
        duration += 5.0
    if syllable_index == max(0, word_syllables - 1):
        duration += 4.0
    return max(82, min(142, round(duration)))


def _english_word_to_moras(
    word: PhoneticWord, *, deaccented: bool,
    resolve_alias: Callable[[Iterable[str]], object | None] | None = None,
) -> list[Mora]:
    moras: list[Mora] = []
    previous_vowel = ""
    word_syllables = max(1, len(word.syllables))
    for syllable_index, syllable in enumerate(word.syllables):
        fallback_onset = _xsampa_onset(syllable.onset)
        dedicated_onset = _xsampa_onset(syllable.onset, dedicated_palatal=True)
        base_vowel = _xsampa_vowel(syllable.vowel)
        coda_source = list(syllable.coda)
        glide = coda_source[0] if coda_source and coda_source[0] in {"j", "w"} else ""
        nasal = syllable.vowel.endswith("~")
        word_start = syllable_index == 0

        # Dedicated PT nasal/palatal aliases are optional: the official English
        # bank lacks them, but a compatible extended bank may supply them. Only
        # choose such a representation when the loaded bank actually has it.
        # An oral aU/aI recording cannot stand for a nasal diphthong: appending
        # N afterwards moves the nasal cue past the glide. Use vowel + N + glide
        # when a dedicated nasal recording is unavailable.
        vowel_options: list[tuple[str, bool, bool]] = []
        if nasal:
            if glide:
                vowel_options.append((syllable.vowel + glide, True, False))
            vowel_options.append((syllable.vowel, False, False))
        elif glide:
            diphthong_alias = _XSAMPA_DIPHTHONGS.get((base_vowel, glide))
            if diphthong_alias:
                vowel_options.append((diphthong_alias, True, False))
        vowel_options.append((base_vowel, False, nasal))
        onset_options = [dedicated_onset] if dedicated_onset != fallback_onset else []
        onset_options.append(fallback_onset)

        selected = None
        if resolve_alias is not None:
            for candidate_vowel, absorbs_glide, needs_nasal_tail in vowel_options:
                for candidate_onset in onset_options:
                    tiers = _english_nucleus_candidates(
                        onset=candidate_onset, vowel=candidate_vowel,
                        previous_vowel=previous_vowel, word_start=word_start,
                    )
                    resolved = _english_resolve(tiers, resolve_alias)
                    candidates = resolved[0]
                    try:
                        available = bool(candidates and resolve_alias((candidates[0],)) is not None)
                    except Exception:
                        available = False
                    if available:
                        selected = (candidate_onset, candidate_vowel, absorbs_glide, needs_nasal_tail, resolved)
                        break
                if selected is not None:
                    break
        if selected is None:
            # With no matching alias (or no bank resolver), preserve every
            # source phone in the fallback plan instead of consuming a glide
            # merely because a diphthong exists in the theoretical inventory.
            tiers = _english_nucleus_candidates(
                onset=fallback_onset, vowel=base_vowel,
                previous_vowel=previous_vowel, word_start=word_start,
            )
            selected = (fallback_onset, base_vowel, False, nasal, _english_resolve(tiers, resolve_alias))
        onset, vowel, absorbs_glide, nasal, resolved = selected
        nucleus_candidates, nucleus_coverage, nucleus_cost = resolved
        if nasal or (dedicated_onset != fallback_onset and onset == fallback_onset):
            # A valid English recording still approximates a PT-only phone.
            # Alias availability must not report that pronunciation as exact.
            nucleus_coverage = "approximation"
        if absorbs_glide:
            coda_source = coda_source[1:]

        # A full-context nucleus owns the onset already. Do not schedule another
        # consonant attack around it; duplicated attacks are especially audible
        # when the bank is used for speech instead of singing.
        full_alias = nucleus_candidates[0] if nucleus_candidates else ""
        compact_alias = full_alias.replace("-", "").replace(" ", "")
        onset_compact = "".join(onset)
        cluster_encoded = bool(onset) and (
            onset_compact in compact_alias
            and (full_alias.startswith("- ") or " " in full_alias or compact_alias.startswith(onset_compact))
        )

        syllable_moras: list[Mora] = []
        if onset and not cluster_encoded:
            syllable_moras.extend(_english_cluster_units(
                onset, word_start=word_start, previous_vowel=previous_vowel,
                resolve_alias=resolve_alias,
                compositional_palatal_cv=(
                    syllable.onset in {("J",), ("L",)}
                    and onset == fallback_onset
                    and compact_alias.endswith(f"{onset[-1]}{vowel}")
                ),
            ))

        syllable_moras.append(Mora(
            candidates=nucleus_candidates,
            duration_ms=_english_nucleus_duration(
                syllable,
                deaccented=deaccented,
                syllable_index=syllable_index,
                word_syllables=word_syllables,
                diphthong=bool(glide),
            ),
            stressed=bool(syllable.stressed and not deaccented),
            deaccented=deaccented,
            role="nucleus",
            source_phonemes=tuple(syllable.onset) + (syllable.vowel,) + (tuple(syllable.coda[:1]) if absorbs_glide else ()),
            coverage=nucleus_coverage,
            profile="english-cvvc",
            planner_cost=nucleus_cost,
        ))

        syllable_moras.extend(_english_coda_units(
            vowel=vowel, coda=tuple(coda_source), nasal=nasal, resolve_alias=resolve_alias,
        ))
        for mora in syllable_moras:
            moras.append(replace(
                mora,
                profile="english-cvvc",
                syllable_index=syllable_index,
                word_syllables=word_syllables,
            ))
        # The bank's next VCV transition references the lexical vowel, not a
        # short coda consonant; this mirrors OpenUtau's prevV behaviour.
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


_VOICEPEAK_PT_WORD = r"[A-Za-zÀÁÂÃÇÉÊÍÓÔÕÚÜàáâãçéêíóôõúü]+"
_VOICEPEAK_KANA = r"[ぁ-ゖァ-ヺー]+"
_VOICEPEAK_DIGITS = ("zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove")


def _voicepeak_portuguese_reading(word: str) -> str:
    deaccented = _is_deaccented_word(word)
    # Use the complete word, rather than the public phonemizer's token/mora
    # limits: validation and size limits belong to the VOICEPEAK caller.
    moras = _phonetic_word_to_moras(g2p_word(word, deaccented=deaccented), deaccented=deaccented)
    if not moras:
        raise ValueError(f"Não há leitura fonética para {word!r} no modo ptbr-kana.")
    reading: list[str] = []
    for mora in moras:
        kana = next((candidate for candidate in mora.candidates if re.fullmatch(_VOICEPEAK_KANA, candidate)), "")
        if not kana:
            raise ValueError(f"Não há aproximação em kana para {word!r} no modo ptbr-kana.")
        # Each planned unit is used once. Nasal/glide helpers already occur in
        # the CV plan; adding them again from source_phonemes would duplicate
        # /nh/, /lh/ and nasal diphthongs.
        reading.append(_katakana_to_hiragana(kana))
    return "".join(reading)


def prepare_voicepeak_text(text: str, mode: str = "ja") -> str:
    """Prepare Japanese text or an experimental PT-BR approximation in kana.

    ``ja`` passes the original text through without translating it. The optional
    ``ptbr-kana`` mode approximates Portuguese sounds with Japanese CV syllables;
    it does not provide native Portuguese pronunciation. Digits are read one at
    a time, and punctuation/whitespace retain sentence and word boundaries.
    Unsupported scripts/symbols fail explicitly rather than disappearing.
    """
    reading_mode = str(mode or "").strip().lower()
    if reading_mode not in {"ja", "ptbr-kana"}:
        raise ValueError(f"Modo de leitura VOICEPEAK inválido: {mode!r}. Use 'ja' ou 'ptbr-kana'.")
    if not isinstance(text, str):
        raise TypeError("O texto VOICEPEAK deve ser uma string.")
    if reading_mode == "ja":
        return text

    normalized = unicodedata.normalize("NFKC", text)
    prepared: list[str] = []
    has_reading = False
    pattern = rf"{_VOICEPEAK_PT_WORD}|\d+|{_VOICEPEAK_KANA}|\s+|."
    for match in re.finditer(pattern, normalized, flags=re.DOTALL):
        token = match.group(0)
        if re.fullmatch(_VOICEPEAK_PT_WORD, token):
            prepared.append(_voicepeak_portuguese_reading(token))
            has_reading = True
        elif token.isdecimal():
            prepared.append(" ".join(_voicepeak_portuguese_reading(_VOICEPEAK_DIGITS[int(digit)]) for digit in token))
            has_reading = True
        elif re.fullmatch(_VOICEPEAK_KANA, token):
            prepared.append(_katakana_to_hiragana(token))
            has_reading = True
        elif token.isspace() or unicodedata.category(token).startswith("P"):
            prepared.append(token)
        else:
            raise ValueError(
                f"Caractere {token!r} na posição {match.start() + 1} não é suportado no modo ptbr-kana. "
                "Use texto em português/kana; para japonês com kanji, use o modo 'ja'."
            )
    if not has_reading:
        raise ValueError("O modo ptbr-kana precisa de texto pronunciável em português, kana ou dígitos.")
    return "".join(prepared)


__all__ = ["Mora", "phonemize", "g2p_word", "prepare_voicepeak_text", "VOICEPEAK_READING_VERSION"]
