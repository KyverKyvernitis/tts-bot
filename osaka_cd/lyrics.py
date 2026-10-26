"""Adapt free lyrics to a CD while retaining the immutable musical template.

Words stay intact for the existing phonemizer. Each syllable and explicit hold
consumes one musical note. All voices stop together when the utterance ends.
Physical v1 requests still respect 128 notes and legal continuous vowel spans.
"""
from __future__ import annotations

import copy
import math

from .arrangement import validate_arrangement, arrangement_sha256
from .phonetics.phonemizer import phonemize

POLICY = 'automatic-lyrics-v2'
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
    """Consume only the requested notes, repeating the motif when necessary."""
    template=validate_arrangement(value)
    units=_units(text,segments,language)
    messages=[part for part in template['parts'] if part.get('lyrics_source')=='message']
    if not messages:
        return template
    slots=sum(unit['nuclei']+unit['holds'] for unit in units)
    period=template['duration_ms']
    selected={}
    end=0
    cycles=1
    for part in messages:
        original=part['notes']
        notes=[dict(original[i%len(original)],
                    start_ms=original[i%len(original)]['start_ms']+period*(i//len(original)))
               for i in range(slots)]
        selected[part['id']]=notes
        end=max(end,notes[-1]['start_ms']+notes[-1]['duration_ms'])
        cycles=max(cycles,math.ceil(slots/len(original)))
    if end>600000:
        raise ValueError('A letra ultrapassa a duração máxima de execução do CD.')
    result=copy.deepcopy(template)
    result['duration_ms']=end
    result['parts']=[]
    omitted=0
    for part in template['parts']:
        if part['id'] in selected:
            notes=selected[part['id']]
        else:
            notes=[]
            for cycle in range(math.ceil(end/period)):
                for original in part['notes']:
                    start=original['start_ms']+period*cycle
                    if start>=end:
                        break
                    length=min(original['duration_ms'],end-start)
                    if length<40:
                        omitted+=1
                        continue
                    notes.append(dict(original,start_ms=start,duration_ms=length))
        if not notes:
            continue
        chosen=dict(part,notes=notes)
        for name in ('source_note_ids','source_note_indices','continuations'):
            chosen.pop(name,None)
        result['parts'].append(chosen)
    source_hash=template.get('source',{}).get('sha256')
    result['source']={'sha256':source_hash} if source_hash else {}
    if omitted:
        warnings=list(result.get('warnings',[]))
        if 'message_boundary_notes_omitted' not in warnings:
            warnings=warnings[:127]+['message_boundary_notes_omitted']
        result['warnings']=warnings
    result['message_lyrics_mode']=POLICY
    result['lyrics_adaptation']={'template_sha256':arrangement_sha256(template),
        'words':len(units),'nuclei':sum(unit['nuclei'] for unit in units),
        'requested_note_slots':slots,'motif_repeats':cycles,'tempo_stretch':1,
        'template_duration_ms':template['duration_ms']}
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
    if arrangement.get('message_lyrics_mode')==POLICY:
        return _finite_render_plan(arrangement,text,segments,language=language)
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
    calls.extend(accompaniment_render_chunks(arrangement))
    calls.sort(key=lambda chunk:chunk['start_ms'])
    return {'chunks':calls,'warnings':['lyrics_automatically_adapted']}


def _finite_render_plan(arrangement,text,segments,*,language):
    """Pack complete words and their finite holds into unchanged v1 requests."""
    units=_units(text,segments,language)
    slots=sum(unit['nuclei']+unit['holds'] for unit in units)
    calls=[]
    for part in arrangement['parts']:
        if part.get('lyrics_source')!='message':
            continue
        if len(part['notes'])!=slots:
            raise ValueError('A execução musical não corresponde à quantidade de notas da letra.')
        pending_notes,pending_segments=[],[]
        pending_moras=0
        anchor=None
        def finish():
            nonlocal pending_moras,anchor
            if not pending_notes:
                return
            start=pending_notes[0]['start_ms']
            score={'version':1,'track_index':part.get('track_index',0),'lyrics_source':'message',
                'duration_ms':pending_notes[-1]['start_ms']+pending_notes[-1]['duration_ms']-start,
                'notes':[dict(note,start_ms=note['start_ms']-start) for note in pending_notes]}
            calls.append({'start_ms':start,'parts':[{'id':part['id'],'role':part['role'],
                'voice_id':part.get('voice_id','kasane-teto'),'score':score,
                'adaptive_text':' '.join(effect['text'] for effect in pending_segments),
                'adaptive_segments':copy.deepcopy(pending_segments)}]})
            pending_notes.clear()
            pending_segments.clear()
            pending_moras=0
            anchor=None
        def fits(notes,effect,moras):
            start=pending_notes[0]['start_ms'] if pending_notes else notes[0]['start_ms']
            return (len(pending_notes)+len(notes)<=128 and pending_moras+moras<=1024
                and notes[-1]['start_ms']+notes[-1]['duration_ms']-start<=60000
                and len(' '.join(e['text'] for e in pending_segments+[effect]))<=2048
                and len(pending_segments)<128)
        def add(notes,word,moras,*,fade_in=False,fade_out=False,hold=False):
            nonlocal pending_moras,anchor
            effect={'text':word,'fade_in':bool(fade_in),'fade_out':bool(fade_out),
                    'hold':bool(hold),'pause_after_ms':0}
            if pending_notes and (not fits(notes,effect,moras)
                or notes[-1]['start_ms']+notes[-1]['duration_ms']-pending_notes[0]['start_ms']>16000):
                finish()
                if hold:
                    return False
            if not fits(notes,effect,moras):
                raise ValueError('Uma palavra ultrapassa a capacidade de um trecho musical de canto.')
            pending_notes.extend(notes)
            pending_segments.append(effect)
            pending_moras+=moras
            if not hold:
                last=notes[-1]
                anchor={'start_ms':last['start_ms'],'low':last['midi_note'],'high':last['midi_note']}
            return True
        def sustain(note,unit,*,last=False):
            nonlocal anchor
            pieces=[item for span in _spans([note]) for item in span]
            for index,piece in enumerate(pieces):
                fade_out=unit['fade_out'] and last and index==len(pieces)-1
                previous=pending_notes[-1] if pending_notes else None
                legal=(anchor is not None and previous is not None
                    and piece['start_ms']-previous['start_ms']-previous['duration_ms']<=20
                    and piece['start_ms']+piece['duration_ms']-anchor['start_ms']<=16000
                    and max(anchor['high'],piece['midi_note'])-min(anchor['low'],piece['midi_note'])<=40)
                if legal and add([piece],'~',0,fade_out=fade_out,hold=True):
                    anchor['low']=min(anchor['low'],piece['midi_note'])
                    anchor['high']=max(anchor['high'],piece['midi_note'])
                else:
                    add([piece],unit['vowel'],unit['vowel_moras'],fade_out=fade_out)
        cursor=0
        for unit in units:
            core=part['notes'][cursor:cursor+unit['nuclei']]
            cursor+=unit['nuclei']
            if len(core)==1 and core[0]['duration_ms']>16000:
                pieces=[item for span in _spans(core) for item in span]
                add([pieces[0]],unit['text'],unit['moras'],fade_in=unit['fade_in'])
                for index,piece in enumerate(pieces[1:]):
                    sustain(piece,unit,last=not unit['holds'] and index==len(pieces)-2)
            else:
                if any(note['duration_ms']>18000 for note in core):
                    raise ValueError('Uma palavra ultrapassa a duração física de um trecho de canto.')
                add(core,unit['text'],unit['moras'],fade_in=unit['fade_in'],
                    fade_out=unit['fade_out'] and not unit['holds'])
            for index in range(unit['holds']):
                sustain(part['notes'][cursor],unit,last=index==unit['holds']-1)
                cursor+=1
            if unit['boundary']:
                finish()
        finish()
    calls.extend(accompaniment_render_chunks(arrangement))
    calls.sort(key=lambda chunk:chunk['start_ms'])
    return {'chunks':calls,'warnings':['lyrics_automatically_adapted']}


def accompaniment_render_chunks(arrangement):
    """Use exactly the guide windows of message singing, then omit its lyrics.

    Keeping the lead while planning is essential: removing it first changes
    shared chunk boundaries and makes warmed stems unusable by real messages.
    """
    from .arrangement import arrangement_render_plan
    guides = copy.deepcopy(arrangement)
    sources = {part['id']: part.get('lyrics_source') for part in guides['parts']}
    for part in guides['parts']:
        if part.get('lyrics_source') == 'message':
            part['lyrics_source'] = 'vowel'
        part['notes'] = [note for span in _spans(part['notes']) for note in span]
        part.pop('source_note_ids', None)
    chunks = []
    for chunk in arrangement_render_plan(guides)['chunks']:
        parts = [request for request in chunk['parts'] if sources[request['id']] != 'message']
        if parts:
            chunks.append(dict(chunk, parts=parts))
    return chunks
