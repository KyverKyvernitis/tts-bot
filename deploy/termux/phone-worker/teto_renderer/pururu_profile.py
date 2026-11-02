"""Optional Pururu speech coarticulation over the stable Flavus planner."""
from __future__ import annotations

from dataclasses import replace
from itertools import groupby

from .phonemizer import Mora, _WORD_GAP_MS, _pururu_phone, english_vowel_source, phonemize


PROFILE_ID = 'pururu-ptbr-v1'


def _recording_candidates(candidates, resolve_alias):
    # Resolve first, rather than assuming that a generic VV/VC spelling exists
    # in this specific bank. Keep the actual OTO alias first for the renderer.
    for candidate in candidates:
        entry = resolve_alias((candidate,))
        if entry is not None:
            return tuple(dict.fromkeys((getattr(entry, 'alias', candidate), *candidates)))
    return ()


def _join_word(previous: list[Mora], current: list[Mora], resolve_alias, *, room: int):
    """Return an authored join, leaving articulated codas/glides intact."""
    if not previous or not current or not current[-1].word_end:
        return current, False
    last = previous[-1]
    if (last.role != 'nucleus' or last.phrase_end
            or last.pause_after_ms > _WORD_GAP_MS):
        return current, False
    source = english_vowel_source(last.source_phonemes)
    if len(source) != 1:
        return current, False
    vowel = _pururu_phone(source[0])
    first = next((mora for mora in current if mora.role == 'nucleus'), None)
    if first is None or not first.source_phonemes:
        return current, False
    onset = tuple(_pururu_phone(phone) for phone in first.source_phonemes[:-1])
    incoming = _pururu_phone(first.source_phonemes[-1])
    joined = list(current)
    if onset:
        if room < 1:
            return current, False
        aliases = _recording_candidates((vowel + onset[0], vowel + ' ' + onset[0]), resolve_alias)
        if not aliases or resolve_alias(first.candidates) is None:
            return current, False
        # The VC belongs to the incoming CV boundary. It is an auxiliary unit,
        # not another syllable, and carries no new stress or word-end pause.
        joined.insert(0, replace(first, candidates=aliases, duration_ms=58,
            pause_after_ms=0, word_end=False, phrase_end='', stressed=False,
            deaccented=True, role='vowel-exit', source_phonemes=(vowel, onset[0]),
            coverage='cvvc-transition', planner_cost=0.0))
    else:
        aliases = _recording_candidates((vowel + ' ' + incoming,), resolve_alias)
        if not aliases:
            return current, False
        index = joined.index(first)
        joined[index] = replace(first, candidates=aliases)
    total = len(joined)
    return [replace(mora, mora_index=index, word_moras=total)
            for index, mora in enumerate(joined)], True


def phonemize_profile(text: str, *, connect_words: bool = True, **options) -> list[Mora]:
    """Add optional real-recording word joins without altering legacy plans.

    Speech callers activate this explicitly for Pururu's versioned profile.
    MIDI callers retain the original planner, whose attacks follow score rests.
    No resolver means no cross-word recording can be assumed to exist.
    """
    native = connect_words is True and options.get('inventory_profile') == 'pururu-purpura'
    # The legacy planner knows periods as 180 ms barriers. Performance callers
    # restore their explicit bar marker after planning the spoken segments.
    clean_text = text.replace('|', '.') if native and isinstance(text, str) else text
    planned = phonemize(clean_text, **options)
    resolve_alias = options.get('resolve_alias')
    if not native or resolve_alias is None:
        return planned
    maximum = max(1, int(options.get('max_moras', 240)))
    remaining = max(0, maximum - len(planned))
    output: list[Mora] = []
    for _, grouped in groupby(planned, key=lambda mora: mora.word_index):
        current = list(grouped)
        joined, linked = _join_word(output, current, resolve_alias, room=remaining)
        if linked:
            remaining -= len(joined) - len(current)
            output[-1] = replace(output[-1], pause_after_ms=0)
        output.extend(joined)
    return output
