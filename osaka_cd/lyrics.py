"""Adapt free lyrics to a CD while retaining the immutable musical template.

Words stay intact for the existing phonemizer. Extra articulations subdivide
notes at the same pitch; short lyrics continue on their last vowel after rests.
Physical v1 requests still respect 128 notes and legal continuous vowel spans.
"""
from __future__ import annotations

import copy
import math

from .arrangement import validate_arrangement, arrangement_sha256
from .phonetics.phonemizer import phonemize

POLICY = 'automatic-lyrics-v1'
NOTE_FLOOR = 120


def _phonemes(text, language):
    return phonemize(text.strip('-'), max_moras=1025,
                     voicebank_profile='english-cvvc', reading_language=language)


def _units(text, segments, language):
    if not isinstance(text, str) or len(text) > 2048 or language not in {'pt-BR','en-US'}:
        raise ValueError('invalid_automatic_lyrics_text_or_language')
    if segments is not None:
        expected={'text','fade_in','fade_out','hold','pause_after_ms'}
        if not isinstance(segments,list) or not 1 <= len(segments) <= 128:
            raise ValueError('invalid_composition_segments')
        for segment in segments:
            if (not isinstance(segment,dict) or set(segment)!=expected
                    or not isinstance(segment['text'],str) or not segment['text'].strip()
                    or len(segment['text'])>2048
                    or any(type(segment[key]) is not bool for key in ('fade_in','fade_out','hold'))
                    or type(segment['pause_after_ms']) is not int or segment['pause_after_ms'] not in {0,180}):
                raise ValueError('invalid_composition_segments')
        joined=' '.join(' '.join(segment['text'].split()) for segment in segments)
        if joined!=' '.join(text.split()):
            raise ValueError('composition_segments_text_mismatch')
    source = segments if segments else [{'text': text}]
    result = []
    parsed = {}
    for segment in source:
        words = segment.get('text', '').split()
        for index, word in enumerate(words):
            if word == '~':
                if result and (segments is None or segment.get('hold')):
                    result[-1]['holds'] += 1
                if result and segment.get('pause_after_ms'):
                    result[-1]['boundary'] = True
                continue
            if word == '|':
                if result and segments is None:
                    result[-1]['boundary'] = True
                continue
            # The physical renderer already strips edge hyphens before G2P.
            word=word.strip('-')
            if word not in parsed:
                phonemes=_phonemes(word,language)
                if len(phonemes)>1024:
                    raise ValueError('Uma palavra ultrapassa a capacidade fonética de um trecho de canto.')
                parsed[word] = (len(phonemes),[unit for unit in phonemes if unit.role=='nucleus'])
            mora_count,nuclei = parsed[word]
            if not nuclei:
                continue
            if len(nuclei) > 128:
                raise ValueError('Uma palavra ultrapassa a capacidade de um trecho de canto.')
            result.append({'text': word, 'nuclei': len(nuclei),
                           'moras':mora_count,
                           'vowel_moras':len(_phonemes(_vowel(nuclei[-1],language),language)),
                           'holds': int(bool(segment.get('hold') and index==len(words)-1)),
                           'fade_in': bool(segment.get('fade_in') and index == 0),
                           'fade_out': bool(segment.get('fade_out') and index == len(words)-1),
                           'boundary': bool(segment.get('pause_after_ms') and index == len(words)-1),
                           'vowel': _vowel(nuclei[-1], language)})
    if not result:
        raise ValueError('A letra precisa conter palavras ou vogais pronunciáveis.')
    return result


def _vowel(mora, language):
    symbols = set(mora.source_phonemes)
    if language == 'en-US':
        candidates = [('i','ee'), ('I','ih'), ('u','oo'), ('U','oo'), ('oU','oh'),
                      ('eI','ay'), ('E','eh'), ('O','aw'), ('aI','eye'), ('aU','ow')]
    else:
        candidates = [('a~','ã'), ('o~','õ'), ('e~','em'), ('i~','im'), ('u~','um'),
                      ('i','i'), ('u','u'), ('e','ê'), ('E','é'), ('o','ô'), ('O','ó')]
    for symbol, spelling in candidates:
        if symbol in symbols:
            return spelling
    return 'a'


def _spans(notes):
    """Independent vowel groups cannot cross rests, 16s or 40 semitones."""
    result, current = [], []
    for original in notes:
        note = {key: original[key] for key in ('midi_note','start_ms','duration_ms','velocity')}
        if 'lyric' in original:
            note['lyric']=original['lyric']
        remaining = note['duration_ms']
        start = note['start_ms']
        pieces = max(1, math.ceil(remaining / 16000))
        for number in range(pieces):
            length = remaining // (pieces-number)
            item = dict(note, start_ms=start, duration_ms=length)
            if current:
                pitches = [entry['midi_note'] for entry in current] + [item['midi_note']]
                previous = current[-1]
                if (item['start_ms']-previous['start_ms']-previous['duration_ms'] > 20
                        or item['start_ms']+length-current[0]['start_ms'] > 16000
                        or max(pitches)-min(pitches)>40 or len(current)>=128):
                    result.append(current)
                    current=[]
            current.append(item)
            remaining -= length
            start += length
    if current:
        result.append(current)
    return result


def _capacity(span):
    return min(128, sum(max(1, note['duration_ms']//NOTE_FLOOR) for note in span))


def _split_notes(span, needed):
    counts = [1]*len(span)
    maximum = [max(1, note['duration_ms']//NOTE_FLOOR) for note in span]
    while sum(counts)<needed:
        available = [i for i in range(len(span)) if counts[i]<maximum[i]]
        if not available:
            raise ValueError('Não foi possível articular a palavra neste trecho musical.')
        index=max(available, key=lambda i: span[i]['duration_ms']/counts[i])
        counts[index]+=1
    result=[]
    for note, count in zip(span,counts):
        cursor=note['start_ms']
        remaining=note['duration_ms']
        for number in range(count):
            length=remaining//(count-number)
            result.append(dict(note,start_ms=cursor,duration_ms=length))
            cursor+=length
            remaining-=length
    return result


def _allocate(notes, units, *, build=False):
    cursor=0
    carry='a'
    carry_moras=8
    spans=[]
    for span in _spans(notes):
        selected=[]
        count=0
        moras=0
        capacity=_capacity(span)
        while (cursor<len(units) and count+units[cursor]['nuclei']<=capacity
               and moras+units[cursor]['moras']<=1024):
            unit=units[cursor]
            selected.append(unit)
            count+=unit['nuclei']
            moras+=unit['moras']
            carry=unit['vowel']
            carry_moras=unit['vowel_moras']
            cursor+=1
            if unit['boundary']:
                break
        if not build and cursor==len(units):
            return cursor,[]
        if build:
            if not selected:
                selected=[{'text':carry,'nuclei':1,'moras':carry_moras,
                           'holds':0,'fade_in':False,'fade_out':False}]
            adapted=_split_notes(span,max(len(span),count))
            spans.append((adapted,selected))
    return cursor,spans


def prepare_message_arrangement(value, text, segments=None, *, language='pt-BR'):
    """Fit complete words by articulating notes or repeating the CD motif."""
    template=validate_arrangement(value)
    units=_units(text,segments,language)
    messages=[part for part in template['parts'] if part.get('lyrics_source')=='message']
    if not messages:
        return template
    maximum_word=max(unit['nuclei'] for unit in units)
    stretch=1
    while True:
        base=copy.deepcopy(template)
        base['duration_ms']*=stretch
        for part in base['parts']:
            for note in part['notes']:
                note['start_ms']*=stretch
                note['duration_ms']*=stretch
        if base['duration_ms']>600000:
            raise ValueError('A adaptação musical ultrapassa a duração máxima de execução.')
        if all(max(_capacity(span) for span in _spans(part['notes']))>=maximum_word
               for part in base['parts'] if part.get('lyrics_source')=='message'):
            break
        stretch+=1
    period=base['duration_ms']
    source_hash=template.get('source',{}).get('sha256')
    base['source']={'sha256':source_hash} if source_hash else {}
    for part in base['parts']:
        for name in ('source_note_ids','source_note_indices','continuations'):
            part.pop(name,None)
    def repeated(cycles):
        result=copy.deepcopy(base)
        result['duration_ms']=period*cycles
        for part in result['parts']:
            original=part['notes']
            part['notes']=[dict(note,start_ms=note['start_ms']+period*cycle)
                           for cycle in range(cycles) for note in original]
        return result
    def fits(result):
        return all(_allocate(part['notes'],units)[0]==len(units) for part in result['parts']
                   if part.get('lyrics_source')=='message')
    maximum_cycles=min(600000//period,24000//sum(len(part['notes']) for part in base['parts']))
    low,cycles=0,1
    result=repeated(cycles)
    while not fits(result):
        if cycles==maximum_cycles:
            raise ValueError('A letra ultrapassa a duração máxima de execução do CD.')
        low=cycles
        cycles=min(maximum_cycles,cycles*2)
        result=repeated(cycles)
    high=cycles
    while high-low>1:
        middle=(low+high)//2
        candidate=repeated(middle)
        if fits(candidate):
            high=middle
        else:
            low=middle
    cycles=high
    result=repeated(cycles)
    result['message_lyrics_mode']=POLICY
    result['lyrics_adaptation']={'template_sha256':arrangement_sha256(template),
        'words':len(units),'nuclei':sum(unit['nuclei'] for unit in units),
        'motif_repeats':cycles,'tempo_stretch':stretch,'template_duration_ms':template['duration_ms']}
    return validate_arrangement(result)


def _tokens(notes, units):
    """Create exact v1 slots; only vowel continuations are automatically added."""
    extra=len(notes)-sum(unit['nuclei'] for unit in units)
    weights=[unit['nuclei']+unit['holds'] for unit in units]
    holds=[0]*len(units)
    for _ in range(extra):
        index=max(range(len(units)),key=lambda i: weights[i]/(holds[i]+1))
        holds[index]+=1
    segments=[]
    for unit,count in zip(units,holds):
        segments.append({'text':unit['text'],'fade_in':unit['fade_in'],
                         'fade_out':unit['fade_out'] and not count,'hold':False,'pause_after_ms':0})
        for number in range(count):
            segments.append({'text':'~','fade_in':False,
                             'fade_out':unit['fade_out'] and number==count-1,
                             'hold':True,'pause_after_ms':0})
    return segments


def automatic_render_plan(arrangement, text, segments=None, *, language='pt-BR'):
    units=_units(text,segments,language)
    calls=[]
    for part in arrangement['parts']:
        if part.get('lyrics_source')!='message':
            continue
        assigned,spans=_allocate(part['notes'],units,build=True)
        if assigned!=len(units):
            raise ValueError('A adaptação musical não preservou todas as palavras.')
        group_notes,group_segments=[],[]
        group_moras=0
        def finish():
            if not group_notes:
                return
            start=group_notes[0]['start_ms']
            notes=[dict(note,start_ms=note['start_ms']-start) for note in group_notes]
            score={'version':1,'track_index':part.get('track_index',0),
                   'lyrics_source':'message','duration_ms':notes[-1]['start_ms']+notes[-1]['duration_ms'],
                   'notes':notes}
            calls.append({'start_ms':start,'parts':[{'id':part['id'],'role':part['role'],
                'voice_id':part.get('voice_id','kasane-teto'),'score':score,
                'adaptive_text':' '.join(item['text'] for item in group_segments),
                'adaptive_segments':copy.deepcopy(group_segments)}]})
            group_notes.clear()
            group_segments.clear()
        for notes,selected in spans:
            effects=_tokens(notes,selected)
            moras=sum(unit['moras'] for unit in selected)
            if len(' '.join(item['text'] for item in effects))>2048:
                raise ValueError('composition_chunk_text_limit')
            start=group_notes[0]['start_ms'] if group_notes else notes[0]['start_ms']
            if group_notes and (len(group_notes)+len(notes)>128
                    or notes[-1]['start_ms']+notes[-1]['duration_ms']-start>16000
                    or group_moras+moras>1024
                    or len(' '.join(item['text'] for item in group_segments+effects))>2048):
                finish()
                group_moras=0
            group_notes.extend(notes)
            group_segments.extend(effects)
            group_moras+=moras
        finish()
    from .arrangement import arrangement_render_plan
    guides=copy.deepcopy(arrangement)
    for part in guides['parts']:
        if part.get('lyrics_source')=='message':
            part['lyrics_source']='vowel'
        split=[]
        for span in _spans(part['notes']):
            split.extend(span)
        part['notes']=split
        part.pop('source_note_ids',None)
    for chunk in arrangement_render_plan(guides)['chunks']:
        parts=[request for request in chunk['parts']
               if next(part for part in arrangement['parts'] if part['id']==request['id']).get('lyrics_source')!='message']
        if parts:
            calls.append(dict(chunk,parts=parts))
    calls.sort(key=lambda chunk:chunk['start_ms'])
    return {'chunks':calls,'warnings':['lyrics_automatically_adapted']}
