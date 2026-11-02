"""Optional Pururu speech joins use authored Flavus links without crossing pauses."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.phonemizer import phonemize as original_phonemize
from teto_renderer.pururu_profile import phonemize_profile as phonemize
from teto_renderer.ptbr_g2p import g2p_word


ALIASES = {'-a', '*a', 'a a', 'u a', 'am', 'mu', 'ak', 'ka', 'az', 'za',
           '-e', 'ew', 'ew a', 'as', 'p6~', '6~ a'}


def resolver(aliases):
    return lambda candidates: next((SimpleNamespace(alias=alias) for alias in candidates
                                    if alias in aliases), None)


def native(text, *, aliases=ALIASES, **options):
    return phonemize(text, inventory_profile='pururu-purpura', reading_language='pt-BR',
                     resolve_alias=resolver(aliases), connect_words=options.pop('connect_words', False), **options)


def lexical(moras):
    return [(m.word_index, m.syllable_index, m.word_syllables, m.stressed,
             m.deaccented, m.source_phonemes) for m in moras if m.role == 'nucleus']


def test_native_vowel_join_replaces_only_word_attack_and_removes_only_synthetic_gap():
    old = native('amo a')
    joined = native('amo a', connect_words=True)
    assert [m.candidates[0] for m in old] == ['-a', 'am', 'mu', '-a']
    assert [m.candidates[0] for m in joined] == ['-a', 'am', 'mu', 'u a']
    assert old[2].pause_after_ms == 6 and joined[2].pause_after_ms == 0
    assert joined[-1].pause_after_ms == 6
    assert lexical(joined) == lexical(old)
    assert all(m.profile == 'english-cvvc' for m in joined), 'physical CVVC layout must stay intact'


def test_native_vc_join_keeps_casa_units_and_lexical_stress():
    old = native('a casa')
    joined = native('a casa', connect_words=True)
    assert [m.candidates[0] for m in joined] == ['-a', 'ak', 'ka', 'az', 'za']
    assert joined[1].role == 'vowel-exit' and joined[1].word_index == 1
    assert [m.candidates[0] for m in joined[2:]] == ['ka', 'az', 'za']
    assert lexical(joined) == lexical(old)
    assert joined[0].pause_after_ms == 0
    assert all(resolver(ALIASES)(m.candidates) is not None for m in joined)


@pytest.mark.parametrize('separator', [',', ';', ':', '.', '!', '?', '\n'])
def test_punctuation_preserves_original_pause_and_word_attack(separator):
    text = 'amo' + separator + ' a'
    old, joined = native(text), native(text, connect_words=True)
    assert [asdict(m) for m in joined] == [asdict(m) for m in old]
    assert joined[2].pause_after_ms > 6
    assert joined[-1].candidates[0] == '-a'


def test_explicit_performance_bar_is_a_pause_and_not_a_cross_word_connection():
    joined = native('amo | a', connect_words=True)
    assert joined[-1].candidates[0] == '-a'
    assert joined[2].pause_after_ms == 180 and joined[2].phrase_end == '.'
    assert lexical(joined) == lexical(native('amo a'))
    assert native('amo | a') == native('amo a'), 'legacy token handling stays unchanged'


@pytest.mark.parametrize(('text', 'missing'), [('amo a', 'u a'), ('a casa', 'ak')])
def test_missing_real_link_keeps_legacy_attack_without_manufacturing_an_alias(text, missing):
    aliases = ALIASES - {missing}
    old, joined = native(text, aliases=aliases), native(text, aliases=aliases, connect_words=True)
    assert joined == old
    assert old[-2 if text == 'amo a' else 0].pause_after_ms == 6


def test_no_bank_resolver_cannot_assume_recorded_connections_exist():
    options = dict(inventory_profile='pururu-purpura', reading_language='pt-BR')
    assert phonemize('amo a casa', connect_words=True, **options) == original_phonemize('amo a casa', **options)


@pytest.mark.parametrize('text', ['eu a', 'as a'])
def test_existing_glides_and_codas_are_not_replayed_in_a_new_word_lead(text):
    old, joined = native(text), native(text, connect_words=True)
    assert joined == old
    assert joined[-1].candidates[0] == '-a'
    assert joined[-2].role in {'glide', 'coda'}


def test_native_nasal_vowel_context_stays_native_and_does_not_add_english_nasal():
    old, joined = native('pã a'), native('pã a', connect_words=True)
    assert [m.candidates[0] for m in joined] == ['p6~', '6~ a']
    assert lexical(joined) == lexical(old)
    assert not any(m.role == 'nasal' for m in joined)


@pytest.mark.parametrize('inventory,profile,language', [
    ('', 'standard', 'pt-BR'), ('', 'english-cvvc', 'pt-BR'),
    ('miku-v4-english', 'english-cvvc', 'pt-BR'),
    ('anna-nyui-diva', 'english-cvvc', 'pt-BR'),
    ('miku-v4-english', 'english-cvvc', 'en-US'),
])
def test_other_voice_plans_ignore_optional_pururu_speech_linking(inventory, profile, language):
    options = dict(inventory_profile=inventory, voicebank_profile=profile, reading_language=language)
    assert phonemize('amo a casa', connect_words=True, **options) == original_phonemize('amo a casa', **options)


def test_explicit_false_is_identical_to_legacy_planning_for_all_native_edge_cases():
    text = 'casa pão mãe banho filha carro claro eu amo a Pururu. Saúde, amor!'
    assert native(text, connect_words=False) == native(text)


def test_vps_and_worker_planners_remain_byte_identical():
    assert (ROOT / 'osaka_cd/phonetics/phonemizer.py').read_bytes() == (
        WORKER / 'teto_renderer/phonemizer.py').read_bytes()
    assert (ROOT / 'osaka_cd/phonetics/pururu_profile.py').read_bytes() == (
        WORKER / 'teto_renderer/pururu_profile.py').read_bytes()


@pytest.mark.skipif(not os.getenv('PURURU_TEST_BANK'), reason='optional real native bank')
def test_real_bank_native_links_keep_all_syllables_and_resolve_every_fragment():
    from teto_renderer.voicebank import VoicebankIndex
    index = VoicebankIndex.load(os.environ['PURURU_TEST_BANK'], minimum_aliases=1000,
                                case_sensitive=True)
    text = 'eu amo a Pururu casa azul pão mãe banho filha carro Brasil'
    options = dict(inventory_profile='pururu-purpura', voicebank_profile='english-cvvc',
                   reading_language='pt-BR', resolve_alias=index.resolve)
    old = phonemize(text, connect_words=False, **options)
    joined = phonemize(text, connect_words=True, **options)
    assert lexical(joined) == lexical(old)
    assert sum(m.role == 'nucleus' for m in joined) == sum(
        len(g2p_word(word).syllables) for word in text.split())
    assert all(index.resolve(m.candidates) is not None for m in joined)
    assert any(m.candidates[0] == 'u a' for m in joined)


def test_auxiliary_connection_never_pushes_plan_over_its_existing_mora_budget():
    # There is room for every old unit but not another auxiliary VC. Keep the
    # old native articulation instead of dropping a lexical nucleus to fit.
    old = original_phonemize('a casa amo', max_moras=6, inventory_profile='pururu-purpura',
                            resolve_alias=resolver(ALIASES))
    joined = phonemize('a casa amo', max_moras=6, connect_words=True,
                      inventory_profile='pururu-purpura', resolve_alias=resolver(ALIASES))
    assert len(joined) <= 6
    assert lexical(joined) == lexical(old)
    assert not any(m.candidates[0] == 'ak' for m in joined)


def test_recording_resolution_can_use_legacy_opaque_availability_objects():
    resolve = lambda candidates: object() if any(alias in ALIASES for alias in candidates) else None
    joined = phonemize('amo a', connect_words=True, inventory_profile='pururu-purpura',
                       resolve_alias=resolve)
    assert joined[-1].candidates[0] == 'u a'
