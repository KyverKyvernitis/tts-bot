"""Native PT-BR recordings preserve Pururu's nasal vowels and palatals."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

import teto_renderer.phonemizer as implementation
from teto_renderer.ptbr_g2p import g2p_word


def resolver(aliases):
    def resolve(candidates):
        return next((SimpleNamespace(alias=alias) for alias in candidates if alias in aliases), None)
    return resolve


def native(text, aliases, **options):
    return implementation.phonemize(text, reading_language='pt-BR',
        inventory_profile='pururu-purpura', resolve_alias=resolver(aliases), **options)


@pytest.mark.parametrize('profile', ['standard', 'english-cvvc'])
def test_casa_uses_the_authored_native_cv_vc_cv_boundary(profile):
    # The bank's own readme gives exactly this example for its CV-VC list.
    moras = native('casa', {'ka', 'az', 'za'}, voicebank_profile=profile)
    assert [mora.candidates[0] for mora in moras] == ['ka', 'az', 'za']
    assert [mora.role for mora in moras] == ['nucleus', 'vowel-exit', 'nucleus']


@pytest.mark.parametrize(('word', 'cv', 'glide'), [
    ('pão', 'p6~', '6~w'), ('mãe', 'm6~', '6~j'), ('muito', 'mu~', 'u~j'),
])
def test_nasal_vowels_are_recorded_in_the_nucleus_without_an_english_nasal_helper(word, cv, glide):
    aliases = {cv, glide, 'u~t', 'u~jt', 'tu'}
    moras = native(word, aliases)
    nuclei = [mora for mora in moras if mora.role == 'nucleus']
    assert nuclei[0].candidates[0] == cv
    assert any(mora.candidates[0] == glide and mora.role == 'glide' for mora in moras)
    assert all(mora.role != 'nasal' for mora in moras)
    assert sum(mora.role == 'nucleus' for mora in moras) == len(g2p_word(word).syllables)
    assert all(resolver(aliases)(mora.candidates) is not None for mora in moras)


@pytest.mark.parametrize(('word', 'aliases', 'required', 'source_phone'), [
    ('banho', {'ba', 'aJ', 'Ju'}, 'Ju', 'J'),
    ('filha', {'fi', 'iL', 'La'}, 'La', 'L'),
    ('carro', {'ka', 'aR', 'Ru'}, 'Ru', 'R'),
    ('caro', {'ka', 'a4', '4u'}, '4u', 'r'),
])
def test_portuguese_palatal_and_r_sounds_use_dedicated_recordings(word, aliases, required, source_phone):
    moras = native(word, aliases)
    nuclei = [mora for mora in moras if mora.role == 'nucleus']
    assert nuclei[-1].candidates[0] == required
    assert source_phone in nuclei[-1].source_phonemes
    assert all(resolver(aliases)(mora.candidates) is not None for mora in moras)
    assert len(nuclei) == 2


@pytest.mark.parametrize(('word', 'aliases', 'first'), [
    ('pé', {'pE'}, 'pE'), ('pó', {'pO'}, 'pO'),
    ('pá', {'pa'}, 'pa'), ('pum', {'pu~'}, 'pu~'),
])
def test_native_oral_and_nasal_vowels_do_not_use_english_vowel_substitutions(word, aliases, first):
    moras = native(word, aliases)
    assert [mora.candidates[0] for mora in moras] == [first]
    assert moras[0].role == 'nucleus'


def test_missing_full_cluster_uses_recorded_cc_and_cv_without_an_extra_syllable():
    aliases = {'p4o', 'ob', 'b l', 'le', 'em', 'ma'}
    moras = native('problema', aliases)
    assert sum(mora.role == 'nucleus' for mora in moras) == 3
    assert any(mora.candidates[0] == 'b l' and mora.role == 'cluster' for mora in moras)
    assert any(mora.candidates[0] == 'le' and mora.role == 'nucleus' for mora in moras)
    assert all(resolver(aliases)(mora.candidates) is not None for mora in moras)


def test_nasal_diphthong_coda_keeps_final_s_when_no_j_s_recording_exists():
    # There is no j+s recording, but the native /e~/+s recording is available.
    aliases = {'be~', 'e~j', 'e~s'}
    moras = native('bens', aliases)
    assert [mora.candidates[0] for mora in moras] == ['be~', 'e~j', 'e~s']
    assert [mora.role for mora in moras] == ['nucleus', 'glide', 'coda']
    assert moras[-1].source_phonemes == ('s',)


@pytest.mark.parametrize(('source', 'steady'), [
    (('a',), '*a'), (('p', 'a~'), '*6~'), (('a~', 'w'), '*6~'),
    (('r', 'e~'), '*e~'), (('dZ', 'i'), '*i'), (('L', 'u'), '*u'),
])
def test_steady_vowels_use_the_authored_star_marker_not_missing_bare_aliases(source, steady):
    assert implementation.pururu_vowel_candidates(source) == (steady,)


@pytest.mark.parametrize('profile', ['standard', 'english-cvvc'])
def test_native_portuguese_bank_rejects_english_instead_of_silently_approximating(profile):
    with pytest.raises(ValueError):
        implementation.phonemize('hello world', voicebank_profile=profile,
            reading_language='en-US', inventory_profile='pururu-purpura')


@pytest.mark.parametrize('text', ['か', 'カ', 'ｶ', 'Olá カ'])
@pytest.mark.parametrize('profile', ['standard', 'english-cvvc'])
def test_native_portuguese_bank_rejects_kana_before_using_another_planner(monkeypatch, text, profile):
    def unexpected(*args, **kwargs):
        pytest.fail('Pururu must reject kana before planning or resolving recordings')
    monkeypatch.setattr(implementation, '_kana_word_to_moras', unexpected)
    monkeypatch.setattr(implementation, 'g2p_word', unexpected)
    with pytest.raises(ValueError, match='português brasileiro'):
        implementation.phonemize(text, voicebank_profile=profile,
            reading_language='pt-BR', inventory_profile='pururu-purpura',
            resolve_alias=unexpected)


def test_vps_and_worker_native_planners_share_source_and_nucleus_counts():
    assert (ROOT / 'osaka_cd/phonetics/phonemizer.py').read_bytes() == (
        WORKER / 'teto_renderer/phonemizer.py').read_bytes()
    from osaka_cd.phonetics.phonemizer import phonemize as shared_phonemize
    text = 'água quase pão mãe muito banho filha'
    options = dict(voicebank_profile='english-cvvc', reading_language='pt-BR',
                   inventory_profile='pururu-purpura')
    worker = implementation.phonemize(text, **options)
    shared = shared_phonemize(text, **options)
    assert [mora.source_phonemes for mora in worker] == [mora.source_phonemes for mora in shared]
    assert sum(mora.role == 'nucleus' for mora in worker) == 12
    assert sum(mora.role == 'nucleus' for mora in worker) == sum(mora.role == 'nucleus' for mora in shared)


@pytest.mark.skipif(not os.getenv('PURURU_TEST_BANK'), reason='optional real Pururu bank not configured')
def test_real_portuguese_recordings_cover_the_native_pronunciation_corpus():
    from teto_renderer.voicebank import VoicebankIndex
    index = VoicebankIndex.load(os.environ['PURURU_TEST_BANK'], minimum_aliases=1000,
                                case_sensitive=True)
    text = ('Olá eu canto com você de coração pão mãe banho filha água quase muito tranquilo '
            'linguiça carro Brasil trabalho saúde táxi perspectiva problema palavra mundo amor '
            'hoje minha nosso nasci olhos chuva trem flor claro também bens irmão irmã órgão')
    moras = implementation.phonemize(text, max_moras=1024, reading_language='pt-BR',
        inventory_profile='pururu-purpura', voicebank_profile='english-cvvc', resolve_alias=index.resolve)
    missing = [(mora.role, mora.source_phonemes, mora.candidates) for mora in moras
               if index.resolve(mora.candidates) is None]
    assert not missing
    assert all(mora.role != 'nasal' for mora in moras)
    assert sum(mora.role == 'nucleus' for mora in moras) == sum(
        len(g2p_word(word).syllables) for word in text.split())
