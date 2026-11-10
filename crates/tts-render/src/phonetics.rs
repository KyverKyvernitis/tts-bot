//! Offline grapheme-to-phoneme conversion ported from the production renderer.
//! The CMU lexicon is an immutable, sorted resource; no Python or network is used.
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::sync::OnceLock;
use unicode_normalization::{char::is_combining_mark, UnicodeNormalization};

/// Stable phonetic planning unit shared by voicebank selection and prosody.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Mora {
    pub candidates: Vec<String>,
    pub duration_ms: f64,
    pub pause_after_ms: f64,
    pub word_end: bool,
    pub phrase_end: String,
    pub word_index: usize,
    pub mora_index: usize,
    pub word_moras: usize,
    pub stressed: bool,
    pub deaccented: bool,
    pub role: String,
    pub source_phonemes: Vec<String>,
    pub coverage: String,
    pub profile: String,
    pub planner_cost: f64,
    pub syllable_index: usize,
    pub word_syllables: usize,
}
impl Default for Mora {
    fn default() -> Self {
        Self {
            candidates: vec![],
            duration_ms: 135.0,
            pause_after_ms: 0.0,
            word_end: false,
            phrase_end: String::new(),
            word_index: 0,
            mora_index: 0,
            word_moras: 1,
            stressed: false,
            deaccented: false,
            role: "nucleus".into(),
            source_phonemes: vec![],
            coverage: "standard-cv".into(),
            profile: "standard".into(),
            planner_cost: 0.0,
            syllable_index: 0,
            word_syllables: 1,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Phone {
    pub symbol: String,
    pub nucleus_index: Option<usize>,
    pub stressed: bool,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Syllable {
    pub onset: Vec<String>,
    pub vowel: String,
    pub coda: Vec<String>,
    pub stressed: bool,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhoneticWord {
    pub source: String,
    pub phones: Vec<Phone>,
    pub syllables: Vec<Syllable>,
}
impl PhoneticWord {
    pub fn phonemes(&self) -> Vec<&str> {
        self.phones.iter().map(|p| p.symbol.as_str()).collect()
    }
}
fn plain(value: &str) -> String {
    value
        .to_lowercase()
        .nfkd()
        .filter(|c| !is_combining_mark(*c) && c.is_alphabetic())
        .collect()
}
fn pt_vowel(c: char) -> bool {
    "aeiouáéíóúâêôãõàü".contains(c)
}
fn explicit(c: char) -> bool {
    "áéíóúâêôãõ".contains(c)
}
fn vowel(s: &str) -> bool {
    matches!(
        s,
        "a" | "a~" | "e" | "e~" | "E" | "i" | "i~" | "o" | "o~" | "O" | "u" | "u~"
    )
}
fn glide(s: &str) -> bool {
    matches!(s, "j" | "w")
}
fn en_vowel(s: &str) -> bool {
    matches!(
        s,
        "A" | "{"
            | "V"
            | "@"
            | "O"
            | "aI"
            | "aU"
            | "E"
            | "3"
            | "eI"
            | "I"
            | "i"
            | "oU"
            | "OI"
            | "U"
            | "u"
    )
}
fn base_vowel(c: char) -> &'static str {
    match c {
        'a' | 'á' | 'à' | 'â' => "a",
        'ã' => "a~",
        'e' | 'ê' => "e",
        'é' => "E",
        'i' | 'í' => "i",
        'o' | 'ô' => "o",
        'ó' => "O",
        'õ' => "o~",
        'u' | 'ú' | 'ü' => "u",
        _ => "a",
    }
}
fn nasal(s: &str) -> &str {
    match s {
        "a" => "a~",
        "e" | "E" => "e~",
        "i" => "i~",
        "o" | "O" => "o~",
        "u" => "u~",
        _ => s,
    }
}
fn pair(c: char, n: Option<char>) -> String {
    let mut s = c.to_string();
    if let Some(n) = n {
        s.push(n);
    }
    s
}
fn orthographic_nuclei(s: &str) -> Vec<usize> {
    let cs: Vec<char> = s.chars().collect();
    let mut out = vec![];
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if !pt_vowel(c) {
            i += 1;
            continue;
        }
        let n = cs.get(i + 1).copied();
        if matches!(c, 'u' | 'ü')
            && i > 0
            && matches!(cs[i - 1], 'g' | 'q')
            && n.is_some_and(pt_vowel)
        {
            i += 1;
            continue;
        }
        out.push(i);
        let p = pair(c, n);
        let pp = if n.is_some_and(pt_vowel) {
            plain(&p)
        } else {
            String::new()
        };
        if n.is_some_and(|n| !explicit(n))
            && (matches!(
                pp.as_str(),
                "ai" | "ei" | "oi" | "ui" | "au" | "eu" | "iu" | "ou"
            ) || matches!(p.as_str(), "ão" | "ãe" | "õe"))
        {
            i += 2;
        } else {
            i += 1;
        }
    }
    out
}
fn stress_nucleus(s: &str, count: usize) -> Option<usize> {
    if count == 0 {
        return None;
    }
    let starts = orthographic_nuclei(s);
    if starts.is_empty() {
        return None;
    }
    let cs: Vec<char> = s.chars().collect();
    let mut group = None;
    for (g, &start) in starts.iter().enumerate() {
        let end = starts.get(g + 1).copied().unwrap_or(cs.len());
        if cs[start..end].iter().any(|c| explicit(*c)) {
            group = Some(g);
            break;
        }
    }
    let g = group.unwrap_or_else(|| {
        let s = plain(s);
        if starts.len() > 1
            && ["a", "e", "o", "as", "es", "os", "am", "em", "ens"]
                .iter()
                .any(|end| s.ends_with(end))
        {
            starts.len() - 2
        } else {
            starts.len() - 1
        }
    });
    Some(if starts.len() <= 1 || count <= 1 {
        0
    } else {
        ((g as f64 * (count - 1) as f64 / (starts.len() - 1) as f64).round_ties_even() as usize)
            .min(count - 1)
    })
}
fn emit_vowel(cs: &[char], i: usize) -> (Vec<String>, usize) {
    let c = cs[i];
    let n = cs.get(i + 1).copied();
    let after = cs.get(i + 2).copied();
    let p = pair(c, n);
    let sym = base_vowel(c);
    let owned = |xs: &[&str]| xs.iter().map(|s| s.to_string()).collect();
    match p.as_str() {
        "ão" => return (owned(&["a~", "w"]), 2),
        "ãe" => return (owned(&["a~", "j"]), 2),
        "õe" => return (owned(&["o~", "j"]), 2),
        _ => {}
    }
    if matches!(n, Some('m' | 'n'))
        && !(n == Some('n') && after == Some('h'))
        && !after.is_some_and(pt_vowel)
    {
        return (owned(&[nasal(sym)]), 2);
    }
    if !n.is_some_and(explicit) {
        match plain(&p).as_str() {
            "ai" | "ei" | "oi" | "ui" => return (owned(&[sym, "j"]), 2),
            "au" | "eu" | "iu" => return (owned(&[sym, "w"]), 2),
            "ou" => return (owned(&[sym]), 2),
            _ => {}
        }
    }
    (owned(&[sym]), 1)
}
fn scan_pt(word: &str) -> Vec<String> {
    let value: String = word
        .to_lowercase()
        .nfc()
        .filter(|c| c.is_ascii_lowercase() || "àáâãçéêíóôõúü".contains(*c))
        .collect();
    let cs: Vec<char> = value.chars().collect();
    let mut out = vec![];
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        let n = cs.get(i + 1).copied();
        let n2 = cs.get(i + 2).copied();
        let prev = if i > 0 { Some(cs[i - 1]) } else { None };
        if pt_vowel(c) {
            let (mut xs, used) = emit_vowel(&cs, i);
            out.append(&mut xs);
            i += used;
            continue;
        }
        let p = pair(c, n);
        if let Some(sym) = match p.as_str() {
            "nh" => Some("J"),
            "lh" => Some("L"),
            "ch" => Some("S"),
            "rr" => Some("R"),
            "ss" => Some("s"),
            _ => None,
        } {
            out.push(sym.into());
            i += 2;
            continue;
        }
        if matches!(c, 'q' | 'g') && matches!(n, Some('u' | 'ü')) && n2.is_some_and(pt_vowel) {
            out.push(if c == 'q' { "k" } else { "g" }.into());
            if n == Some('ü')
                || !n2.is_some_and(|c| "eiéêí".contains(c))
                || matches!(
                    value.as_str(),
                    "tranquilo"
                        | "tranquila"
                        | "tranquilos"
                        | "tranquilas"
                        | "linguiça"
                        | "linguiças"
                )
            {
                out.push("w".into());
            }
            i += 2;
            continue;
        }
        if matches!(p.as_str(), "sc" | "sç" | "xc") && n2.is_some_and(|c| "eiéêí".contains(c)) {
            out.push("s".into());
            i += 2;
            continue;
        }
        let front = n.is_some_and(|c| "eiéêí".contains(c));
        let palatal = n.is_some_and(|c| "ií".contains(c)) || (n == Some('e') && i + 2 == cs.len());
        let sym = match c {
            'b' => "b",
            'ç' => "s",
            'c' => {
                if front {
                    "s"
                } else {
                    "k"
                }
            }
            'd' => {
                if palatal {
                    "dZ"
                } else {
                    "d"
                }
            }
            'f' => "f",
            'g' => {
                if front {
                    "Z"
                } else {
                    "g"
                }
            }
            'h' => "",
            'j' => "Z",
            'k' | 'q' => "k",
            'l' => {
                if !n.is_some_and(pt_vowel) {
                    "w"
                } else {
                    "l"
                }
            }
            'm' => "m",
            'n' => "n",
            'p' => "p",
            'r' => {
                if i == 0 || prev.is_some_and(|c| "nls".contains(c)) {
                    "R"
                } else {
                    "r"
                }
            }
            's' => {
                if prev.is_some_and(pt_vowel) && n.is_some_and(pt_vowel) {
                    "z"
                } else {
                    "s"
                }
            }
            't' => {
                if palatal {
                    "tS"
                } else {
                    "t"
                }
            }
            'v' => "v",
            'w' => "w",
            'x' => {
                if i == 1 && value.starts_with('e') && n.is_some_and(pt_vowel) {
                    "z"
                } else {
                    "S"
                }
            }
            'y' => "j",
            'z' => {
                if n.is_none() {
                    "s"
                } else {
                    "z"
                }
            }
            _ => "",
        };
        if !sym.is_empty() {
            out.push(sym.into());
        }
        i += 1;
    }
    out
}
fn pt_cluster(xs: &[String]) -> bool {
    matches!(
        xs.iter().map(String::as_str).collect::<Vec<_>>().as_slice(),
        ["p", "r"]
            | ["b", "r"]
            | ["t", "r"]
            | ["d", "r"]
            | ["k", "r"]
            | ["g", "r"]
            | ["f", "r"]
            | ["v", "r"]
            | ["p", "l"]
            | ["b", "l"]
            | ["k", "l"]
            | ["g", "l"]
            | ["f", "l"]
            | ["v", "l"]
            | ["k", "w"]
            | ["g", "w"]
    )
}
fn syllabify_pt(xs: &[String], stress: Option<usize>) -> Vec<Syllable> {
    let positions: Vec<usize> = xs
        .iter()
        .enumerate()
        .filter(|(_, s)| vowel(s))
        .map(|(i, _)| i)
        .collect();
    let mut out: Vec<Syllable> = vec![];
    let mut previous = 0;
    for (num, &pos) in positions.iter().enumerate() {
        let between = &xs[previous..pos];
        let split = if num == 0 {
            0
        } else if between.len() >= 2 && pt_cluster(&between[between.len() - 2..]) {
            between.len() - 2
        } else if !between.is_empty() {
            between.len() - 1
        } else {
            0
        };
        if num > 0 {
            out.last_mut()
                .unwrap()
                .coda
                .extend_from_slice(&between[..split]);
        }
        let next = positions.get(num + 1).copied().unwrap_or(xs.len());
        let mut cursor = pos + 1;
        while cursor < next && glide(&xs[cursor]) {
            cursor += 1;
        }
        out.push(Syllable {
            onset: between[split..].to_vec(),
            vowel: xs[pos].clone(),
            coda: xs[pos + 1..cursor].to_vec(),
            stressed: Some(num) == stress,
        });
        previous = cursor;
    }
    if let Some(&last) = positions.last() {
        let mut cursor = last + 1;
        while cursor < xs.len() && glide(&xs[cursor]) {
            cursor += 1;
        }
        out.last_mut()
            .unwrap()
            .coda
            .extend_from_slice(&xs[cursor..]);
    }
    out
}
pub fn g2p_word(word: &str, deaccented: bool) -> PhoneticWord {
    let source: String = word.nfc().collect::<String>().trim().into();
    let low = source.to_lowercase();
    let lex: Option<&[&str]> = match low.as_str() {
        "miku" => Some(&["m", "i", "k", "u"]),
        "pode" => Some(&["p", "O", "dZ", "i"]),
        "nossa" => Some(&["n", "O", "s", "a"]),
        "táxi" => Some(&["t", "a", "k", "s", "i"]),
        "fixo" => Some(&["f", "i", "k", "s", "u"]),
        "tóxico" => Some(&["t", "O", "k", "s", "i", "k", "u"]),
        "próximo" => Some(&["p", "r", "O", "s", "i", "m", "u"]),
        "muito" => Some(&["m", "u~", "j", "t", "u"]),
        "muita" => Some(&["m", "u~", "j", "t", "a"]),
        "muitos" => Some(&["m", "u~", "j", "t", "u", "s"]),
        "muitas" => Some(&["m", "u~", "j", "t", "a", "s"]),
        _ => None,
    };
    let mut xs = lex
        .map(|p| p.iter().map(|s| s.to_string()).collect())
        .unwrap_or_else(|| scan_pt(&source));
    let count = xs.iter().filter(|s| vowel(s)).count();
    let stress = if deaccented {
        None
    } else if lex.is_some() {
        Some(0)
    } else {
        stress_nucleus(&low, count)
    };
    if count > 0 && Some(count - 1) != stress {
        if let Some(pos) = xs.iter().rposition(|s| vowel(s)) {
            xs[pos] = match xs[pos].as_str() {
                "e" => "i".into(),
                "o" => "u".into(),
                _ => xs[pos].clone(),
            };
        }
    }
    if ["em", "ém", "ens", "éns"]
        .iter()
        .any(|end| low.ends_with(end))
    {
        if let Some(pos) = xs.iter().rposition(|s| vowel(s)) {
            if xs[pos] == "e~"
                && (low.ends_with("ens") || low.ends_with("éns") || stress == Some(count - 1))
            {
                xs.insert(pos + 1, "j".into());
            }
        }
    }
    let mut nucleus = 0;
    let phones = xs
        .iter()
        .map(|symbol| {
            let idx = if vowel(symbol) {
                let i = nucleus;
                nucleus += 1;
                Some(i)
            } else {
                None
            };
            Phone {
                symbol: symbol.clone(),
                nucleus_index: idx,
                stressed: idx.is_some() && idx == stress,
            }
        })
        .collect();
    PhoneticWord {
        source,
        phones,
        syllables: syllabify_pt(&xs, stress),
    }
}
fn dictionary() -> &'static Vec<(&'static str, &'static str)> {
    static DICT: OnceLock<Vec<(&'static str, &'static str)>> = OnceLock::new();
    DICT.get_or_init(|| {
        include_str!("../data/cmudict.txt")
            .lines()
            .filter_map(|line| line.split_once(' '))
            .collect()
    })
}
fn pronunciation(key: &str) -> Option<&'static str> {
    let dict = dictionary();
    dict.binary_search_by_key(&key, |(word, _)| *word)
        .ok()
        .map(|i| dict[i].1)
}
pub fn english_dictionary_word_count() -> usize {
    dictionary().len()
}
fn en_cluster(xs: &[String]) -> bool {
    matches!(
        xs.iter().map(String::as_str).collect::<Vec<_>>().as_slice(),
        ["p", "r"]
            | ["b", "r"]
            | ["t", "r"]
            | ["d", "r"]
            | ["k", "r"]
            | ["g", "r"]
            | ["f", "r"]
            | ["T", "r"]
            | ["S", "r"]
            | ["p", "l"]
            | ["b", "l"]
            | ["k", "l"]
            | ["g", "l"]
            | ["f", "l"]
            | ["s", "l"]
            | ["s", "m"]
            | ["s", "n"]
            | ["s", "p"]
            | ["s", "t"]
            | ["s", "k"]
            | ["s", "w"]
            | ["t", "w"]
            | ["k", "w"]
            | ["g", "w"]
            | ["s", "p", "r"]
            | ["s", "t", "r"]
            | ["s", "k", "r"]
            | ["s", "p", "l"]
            | ["s", "k", "l"]
            | ["s", "k", "w"]
    )
}
fn syllabify_en(xs: &[String], stress: &BTreeSet<usize>) -> Vec<Syllable> {
    let positions: Vec<usize> = xs
        .iter()
        .enumerate()
        .filter(|(_, s)| en_vowel(s))
        .map(|(i, _)| i)
        .collect();
    let mut out: Vec<Syllable> = vec![];
    let mut previous = 0;
    for (num, &pos) in positions.iter().enumerate() {
        let between = &xs[previous..pos];
        let split = if num == 0 {
            0
        } else {
            let mut size = usize::from(between.last().is_some_and(|s| s != "N"));
            for n in [2, 3] {
                if between.len() >= n && en_cluster(&between[between.len() - n..]) {
                    size = n;
                }
            }
            let split = between.len() - size;
            out.last_mut().unwrap().coda = between[..split].to_vec();
            split
        };
        out.push(Syllable {
            onset: between[split..].to_vec(),
            vowel: xs[pos].clone(),
            coda: vec![],
            stressed: stress.contains(&num),
        });
        previous = pos + 1;
    }
    if let Some(&last) = positions.last() {
        out.last_mut().unwrap().coda = xs[last + 1..].to_vec();
    }
    out
}
fn arpabet_vowel(base: &str) -> Option<&'static str> {
    Some(match base {
        "AA" => "A",
        "AE" => "{",
        "AH" => "V",
        "AO" => "O",
        "AW" => "aU",
        "AY" => "aI",
        "EH" => "E",
        "ER" => "3",
        "EY" => "eI",
        "IH" => "I",
        "IY" => "i",
        "OW" => "oU",
        "OY" => "OI",
        "UH" => "U",
        "UW" => "u",
        _ => return None,
    })
}
fn arpabet_consonant(base: &str) -> Option<&'static str> {
    Some(match base {
        "B" => "b",
        "CH" => "tS",
        "D" => "d",
        "DH" => "D",
        "F" => "f",
        "G" => "g",
        "HH" => "h",
        "JH" => "dZ",
        "K" => "k",
        "L" => "l",
        "M" => "m",
        "N" => "n",
        "NG" => "N",
        "P" => "p",
        "R" => "r",
        "S" => "s",
        "SH" => "S",
        "T" => "t",
        "TH" => "T",
        "V" => "v",
        "W" => "w",
        "Y" => "j",
        "Z" => "z",
        "ZH" => "Z",
        _ => return None,
    })
}
fn from_arpabet(pron: &str) -> (Vec<String>, BTreeSet<usize>) {
    let (mut xs, mut primary, mut secondary) = (vec![], BTreeSet::new(), BTreeSet::new());
    let mut nucleus = 0;
    for p in pron.split_whitespace() {
        let base = p.trim_end_matches(['0', '1', '2']);
        if let Some(v) = arpabet_vowel(base) {
            let stress = p
                .chars()
                .last()
                .filter(|c| matches!(c, '0' | '1' | '2'))
                .unwrap_or('0');
            xs.push(
                if stress == '0' && matches!(base, "AH" | "ER") {
                    "@"
                } else {
                    v
                }
                .into(),
            );
            if stress == '1' {
                primary.insert(nucleus);
            } else if stress == '2' {
                secondary.insert(nucleus);
            }
            nucleus += 1;
            if base == "ER" {
                xs.push("r".into());
            }
        } else if let Some(c) = arpabet_consonant(base) {
            xs.push(c.into());
        }
    }
    (
        xs,
        if primary.is_empty() {
            secondary
        } else {
            primary
        },
    )
}
fn letter_name(c: char) -> &'static str {
    match c {
        'a' => "EY1",
        'b' => "B IY1",
        'c' => "S IY1",
        'd' => "D IY1",
        'e' => "IY1",
        'f' => "EH1 F",
        'g' => "JH IY1",
        'h' => "EY1 CH",
        'i' => "AY1",
        'j' => "JH EY1",
        'k' => "K EY1",
        'l' => "EH1 L",
        'm' => "EH1 M",
        'n' => "EH1 N",
        'o' => "OW1",
        'p' => "P IY1",
        'q' => "K Y UW1",
        'r' => "AA1 R",
        's' => "EH1 S",
        't' => "T IY1",
        'u' => "Y UW1",
        'v' => "V IY1",
        'w' => "D AH1 B AH0 L Y UW0",
        'x' => "EH1 K S",
        'y' => "W AY1",
        'z' => "Z IY1",
        _ => "",
    }
}
fn inflected(word: &str) -> Option<String> {
    for end in ["'s", "ing", "ed", "es", "s"] {
        if !word.ends_with(end) || word.len() <= end.len() + 1 {
            continue;
        }
        let stem = &word[..word.len() - end.len()];
        let mut candidates = vec![stem.to_string()];
        if matches!(end, "ing" | "ed") {
            candidates.push(format!("{stem}e"));
            let cs: Vec<char> = stem.chars().collect();
            if cs.len() >= 2 && cs[cs.len() - 1] == cs[cs.len() - 2] {
                candidates.push(cs[..cs.len() - 1].iter().collect());
            }
        }
        if matches!(end, "es" | "ed") && stem.ends_with('i') {
            candidates.push(format!("{}y", &stem[..stem.len() - 1]));
        }
        let Some(pron) = candidates.iter().find_map(|s| pronunciation(s)) else {
            continue;
        };
        let last = pron.split_whitespace().last().unwrap_or("");
        let suffix = if end == "ing" {
            "IH0 NG"
        } else if end == "ed" {
            if matches!(last, "T" | "D") {
                "IH0 D"
            } else if matches!(last, "P" | "K" | "F" | "S" | "SH" | "CH" | "TH") {
                "T"
            } else {
                "D"
            }
        } else if matches!(last, "S" | "Z" | "SH" | "ZH" | "CH" | "JH") {
            "IH0 Z"
        } else if matches!(last, "P" | "T" | "K" | "F" | "TH") {
            "S"
        } else {
            "Z"
        };
        return Some(format!("{pron} {suffix}"));
    }
    None
}
fn oov(word: &str) -> Vec<String> {
    let value: String = plain(word)
        .chars()
        .filter(|c| c.is_ascii_lowercase())
        .collect();
    if value.is_empty() {
        return vec![];
    }
    let digraphs: &[(&str, &[&str])] = &[
        ("tion", &["S", "@", "n"]),
        ("sion", &["Z", "@", "n"]),
        ("tch", &["tS"]),
        ("dge", &["dZ"]),
        ("igh", &["aI"]),
        ("sh", &["S"]),
        ("ch", &["tS"]),
        ("th", &["T"]),
        ("ph", &["f"]),
        ("ng", &["N"]),
        ("ck", &["k"]),
        ("wh", &["w"]),
        ("qu", &["k", "w"]),
        ("ee", &["i"]),
        ("ea", &["i"]),
        ("oo", &["u"]),
        ("oa", &["oU"]),
        ("ai", &["eI"]),
        ("ay", &["eI"]),
        ("ei", &["eI"]),
        ("ey", &["eI"]),
        ("oi", &["OI"]),
        ("oy", &["OI"]),
        ("ou", &["aU"]),
        ("ow", &["aU"]),
        ("au", &["O"]),
        ("aw", &["O"]),
        ("er", &["3", "r"]),
        ("ir", &["3", "r"]),
        ("ur", &["3", "r"]),
        ("ar", &["A", "r"]),
        ("or", &["O", "r"]),
    ];
    let b = value.as_bytes();
    let silent = b.len() > 2 && value.ends_with('e') && !b"aeiou".contains(&b[b.len() - 2]);
    let mut i = usize::from(["kn", "gn", "wr"].iter().any(|s| value.starts_with(s)));
    let mut out: Vec<String> = vec![];
    while i < b.len() {
        if silent && i == b.len() - 1 {
            break;
        }
        if let Some((chunk, xs)) = digraphs
            .iter()
            .find(|(chunk, _)| value[i..].starts_with(chunk))
        {
            out.extend(xs.iter().map(|s| s.to_string()));
            i += chunk.len();
            continue;
        }
        let c = b[i] as char;
        let n = b.get(i + 1).copied().map(char::from);
        let long = silent && i == b.len() - 3;
        let sym = match c {
            'a' => {
                if long {
                    "eI"
                } else {
                    "{"
                }
            }
            'e' => {
                if long {
                    "i"
                } else {
                    "E"
                }
            }
            'i' => {
                if long {
                    "aI"
                } else {
                    "I"
                }
            }
            'o' => {
                if long {
                    "oU"
                } else {
                    "A"
                }
            }
            'u' => {
                if long {
                    "u"
                } else {
                    "V"
                }
            }
            'c' => {
                if n.is_some_and(|c| "eiy".contains(c)) {
                    "s"
                } else {
                    "k"
                }
            }
            'g' => {
                if n.is_some_and(|c| "eiy".contains(c)) {
                    "dZ"
                } else {
                    "g"
                }
            }
            'y' => {
                if i == 0 {
                    "j"
                } else if i == b.len() - 1 {
                    "i"
                } else {
                    "I"
                }
            }
            'j' => "dZ",
            'q' => "k",
            'x' => {
                out.push("k".into());
                "s"
            }
            'b' => "b",
            'd' => "d",
            'f' => "f",
            'h' => "h",
            'k' => "k",
            'l' => "l",
            'm' => "m",
            'n' => "n",
            'p' => "p",
            'r' => "r",
            's' => "s",
            't' => "t",
            'v' => "v",
            'w' => "w",
            'z' => "z",
            _ => "",
        };
        if !sym.is_empty() {
            out.push(sym.into());
        }
        i += if n == Some(c) && !"aeiou".contains(c) {
            2
        } else {
            1
        };
    }
    if !out.iter().any(|s| en_vowel(s)) {
        from_arpabet(&value.chars().map(letter_name).collect::<Vec<_>>().join(" ")).0
    } else {
        out
    }
}
pub fn g2p_english_word(word: &str, deaccented: bool) -> PhoneticWord {
    let source = word.nfc().collect::<String>().trim().replace('’', "'");
    let key = source.to_lowercase();
    let mut pron = match key.as_str() {
        "miku" => Some("M IY1 K UW0".to_string()),
        "teto" => Some("T EH1 T OW0".to_string()),
        _ => pronunciation(&key).map(str::to_string),
    };
    if pron.is_none()
        && source.chars().count() > 1
        && source.chars().count() <= 8
        && source.chars().all(char::is_alphabetic)
        && source.chars().any(char::is_uppercase)
        && source.chars().all(|c| !c.is_lowercase())
    {
        pron = Some(
            key.chars()
                .map(letter_name)
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" "),
        );
    }
    if pron.is_none() {
        pron = inflected(&key);
    }
    let (xs, mut stress) = if let Some(p) = pron.filter(|p| !p.is_empty()) {
        from_arpabet(&p)
    } else {
        let xs = oov(&key);
        let stress = if xs.iter().any(|s| en_vowel(s)) {
            BTreeSet::from([0])
        } else {
            BTreeSet::new()
        };
        (xs, stress)
    };
    if deaccented {
        stress.clear();
    }
    let mut nucleus = 0;
    let phones = xs
        .iter()
        .map(|s| {
            let i = if en_vowel(s) {
                let i = nucleus;
                nucleus += 1;
                Some(i)
            } else {
                None
            };
            Phone {
                symbol: s.clone(),
                nucleus_index: i,
                stressed: i.is_some_and(|i| stress.contains(&i)),
            }
        })
        .collect();
    PhoneticWord {
        source,
        phones,
        syllables: syllabify_en(&xs, &stress),
    }
}
const SMALL: [&str; 20] = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
];
fn integer(n: u64) -> String {
    if n < 20 {
        return SMALL[n as usize].into();
    }
    if n < 100 {
        let tens = [
            "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        ];
        return format!(
            "{}{}",
            tens[(n / 10) as usize],
            if n % 10 > 0 {
                format!(" {}", integer(n % 10))
            } else {
                String::new()
            }
        );
    }
    for (scale, name) in [
        (1_000_000_000_000, "trillion"),
        (1_000_000_000, "billion"),
        (1_000_000, "million"),
        (1000, "thousand"),
        (100, "hundred"),
    ] {
        if n >= scale {
            return format!(
                "{} {}{}",
                integer(n / scale),
                name,
                if n % scale > 0 {
                    format!(" {}", integer(n % scale))
                } else {
                    String::new()
                }
            );
        }
    }
    String::new()
}
pub fn expand_english_numbers(text: &str) -> String {
    // Rust regex omits look-behind. Check surrounding characters explicitly.
    static GROUPED: OnceLock<regex::Regex> = OnceLock::new();
    static NUMBERS: OnceLock<regex::Regex> = OnceLock::new();
    let grouped = GROUPED.get_or_init(|| regex::Regex::new(r"\d{1,3}(?:,\d{3})+").unwrap());
    let mut normalized = String::new();
    let mut previous = 0;
    for m in grouped.find_iter(text) {
        let before = text[..m.start()].chars().next_back();
        let after = text[m.end()..].chars().next();
        if !before.is_some_and(|c| c.is_numeric()) && !after.is_some_and(|c| c.is_numeric()) {
            normalized.push_str(&text[previous..m.start()]);
            normalized.push_str(&m.as_str().replace(',', ""));
            previous = m.end();
        }
    }
    normalized.push_str(&text[previous..]);
    let numbers = NUMBERS.get_or_init(|| regex::Regex::new(r"-?[0-9]+(?:\.[0-9]+)?").unwrap());
    let mut out = String::new();
    previous = 0;
    for m in numbers.find_iter(&normalized) {
        if normalized[..m.start()]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_alphanumeric() || c == '_')
        {
            continue;
        }
        out.push_str(&normalized[previous..m.start()]);
        let neg = m.as_str().starts_with('-');
        let val = m.as_str().trim_start_matches('-');
        let (whole, frac) = val
            .split_once('.')
            .map(|(w, f)| (w, Some(f)))
            .unwrap_or((val, None));
        let spoken = if whole.len() > 15 || (whole.len() > 1 && whole.starts_with('0')) {
            whole
                .bytes()
                .map(|d| SMALL[(d - b'0') as usize])
                .collect::<Vec<_>>()
                .join(" ")
        } else {
            integer(whole.parse().unwrap_or(0))
        };
        if neg {
            out.push_str("minus ");
        }
        out.push_str(&spoken);
        if let Some(frac) = frac {
            out.push_str(" point ");
            out.push_str(
                &frac
                    .bytes()
                    .map(|d| SMALL[(d - b'0') as usize])
                    .collect::<Vec<_>>()
                    .join(" "),
            );
        }
        previous = m.end();
    }
    out.push_str(&normalized[previous..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Deserialize)]
    struct Contract {
        language: String,
        word: String,
        deaccented: bool,
        expected: PhoneticWord,
    }
    #[test]
    fn production_g2p_contracts() {
        let cases: Vec<Contract> =
            serde_json::from_str(include_str!("../data/g2p-contracts.json")).unwrap();
        for case in cases {
            let actual = if case.language == "pt-BR" {
                g2p_word(&case.word, case.deaccented)
            } else {
                g2p_english_word(&case.word, case.deaccented)
            };
            assert_eq!(
                actual, case.expected,
                "{} {:?} deaccented={}",
                case.language, case.word, case.deaccented
            );
        }
    }
    #[test]
    fn immutable_dictionary_is_complete_sorted_unique() {
        assert_eq!(english_dictionary_word_count(), 126052);
        assert!(dictionary().windows(2).all(|p| p[0].0 < p[1].0));
    }
    #[test]
    fn english_number_reading() {
        assert_eq!(expand_english_numbers("I have -12.05 and 1,234 cats, code A123 007"),"I have minus twelve point zero five and one thousand two hundred thirty four cats, code A123 zero zero seven");
    }
    #[test]
    fn normalization_preserves_lexical_accent() {
        assert_ne!(
            g2p_word("pode", false).phonemes(),
            g2p_word("pôde", false).phonemes()
        );
        assert_eq!(g2p_word("pôde", false), g2p_word("pôde", false));
    }
}

// Voicebank spelling tables preserved in their original candidate order.
const ROMAJI_TO_KANA: &[(&str, &str)] = &[
    ("kya", "きゃ"),
    ("kyu", "きゅ"),
    ("kyo", "きょ"),
    ("gya", "ぎゃ"),
    ("gyu", "ぎゅ"),
    ("gyo", "ぎょ"),
    ("sha", "しゃ"),
    ("shu", "しゅ"),
    ("sho", "しょ"),
    ("sya", "しゃ"),
    ("syu", "しゅ"),
    ("syo", "しょ"),
    ("ja", "じゃ"),
    ("ju", "じゅ"),
    ("jo", "じょ"),
    ("jya", "じゃ"),
    ("jyu", "じゅ"),
    ("jyo", "じょ"),
    ("cha", "ちゃ"),
    ("chu", "ちゅ"),
    ("cho", "ちょ"),
    ("tya", "ちゃ"),
    ("tyu", "ちゅ"),
    ("tyo", "ちょ"),
    ("nya", "にゃ"),
    ("nyu", "にゅ"),
    ("nyo", "にょ"),
    ("hya", "ひゃ"),
    ("hyu", "ひゅ"),
    ("hyo", "ひょ"),
    ("bya", "びゃ"),
    ("byu", "びゅ"),
    ("byo", "びょ"),
    ("pya", "ぴゃ"),
    ("pyu", "ぴゅ"),
    ("pyo", "ぴょ"),
    ("mya", "みゃ"),
    ("myu", "みゅ"),
    ("myo", "みょ"),
    ("rya", "りゃ"),
    ("ryu", "りゅ"),
    ("ryo", "りょ"),
    ("fa", "ふぁ"),
    ("fi", "ふぃ"),
    ("fe", "ふぇ"),
    ("fo", "ふぉ"),
    ("tsa", "つぁ"),
    ("tsi", "つぃ"),
    ("tse", "つぇ"),
    ("tso", "つぉ"),
    ("she", "しぇ"),
    ("che", "ちぇ"),
    ("je", "じぇ"),
    ("ka", "か"),
    ("ki", "き"),
    ("ku", "く"),
    ("ke", "け"),
    ("ko", "こ"),
    ("ga", "が"),
    ("gi", "ぎ"),
    ("gu", "ぐ"),
    ("ge", "げ"),
    ("go", "ご"),
    ("sa", "さ"),
    ("shi", "し"),
    ("si", "し"),
    ("su", "す"),
    ("se", "せ"),
    ("so", "そ"),
    ("za", "ざ"),
    ("ji", "じ"),
    ("zi", "じ"),
    ("zu", "ず"),
    ("ze", "ぜ"),
    ("zo", "ぞ"),
    ("ta", "た"),
    ("chi", "ち"),
    ("ti", "ち"),
    ("tsu", "つ"),
    ("tu", "つ"),
    ("te", "て"),
    ("to", "と"),
    ("da", "だ"),
    ("di", "でぃ"),
    ("du", "どぅ"),
    ("de", "で"),
    ("do", "ど"),
    ("na", "な"),
    ("ni", "に"),
    ("nu", "ぬ"),
    ("ne", "ね"),
    ("no", "の"),
    ("ha", "は"),
    ("hi", "ひ"),
    ("fu", "ふ"),
    ("hu", "ふ"),
    ("he", "へ"),
    ("ho", "ほ"),
    ("ba", "ば"),
    ("bi", "び"),
    ("bu", "ぶ"),
    ("be", "べ"),
    ("bo", "ぼ"),
    ("pa", "ぱ"),
    ("pi", "ぴ"),
    ("pu", "ぷ"),
    ("pe", "ぺ"),
    ("po", "ぽ"),
    ("ma", "ま"),
    ("mi", "み"),
    ("mu", "む"),
    ("me", "め"),
    ("mo", "も"),
    ("ya", "や"),
    ("yu", "ゆ"),
    ("yo", "よ"),
    ("ra", "ら"),
    ("ri", "り"),
    ("ru", "る"),
    ("re", "れ"),
    ("ro", "ろ"),
    ("wa", "わ"),
    ("wi", "うぃ"),
    ("we", "うぇ"),
    ("wo", "を"),
    ("a", "あ"),
    ("i", "い"),
    ("u", "う"),
    ("e", "え"),
    ("o", "お"),
    ("n", "ん"),
];
const MIKU_V4_VOWELS: &[(&str, &str)] = &[
    ("a", "V"),
    ("A", "Q"),
    ("E", "e"),
    ("i", "i:"),
    ("u", "u:"),
    ("o", "O:"),
    ("O", "O:"),
    ("oU", "@U"),
    ("3", "@r"),
];
const ANNA_DIVA_VOWELS: &[(&str, &str)] = &[
    ("A", "a"),
    ("{", "@"),
    ("V", "u"),
    ("O", "9"),
    ("aU", "8"),
    ("aI", "I"),
    ("E", "e"),
    ("3", "3"),
    ("eI", "A"),
    ("I", "i"),
    ("i", "E"),
    ("oU", "O"),
    ("OI", "Q"),
    ("U", "6"),
    ("u", "o"),
    ("@", "x"),
    ("o", "9"),
];
const ANNA_DIVA_CONSONANTS: &[(&str, &str)] = &[
    ("dZ", "j"),
    ("tS", "ch"),
    ("D", "dh"),
    ("T", "th"),
    ("S", "sh"),
    ("Z", "zh"),
    ("j", "y"),
    ("N", "ng"),
    ("4", "r"),
];
const ANNA_DIVA_VC_EXCEPTIONS: &[(&str, &str)] = &[
    ("ing", "1ng"),
    ("9r", "0r"),
    ("er", "Ar"),
    ("ir", "Er"),
    ("6r", "or"),
    ("@m", "&m"),
    ("@n", "&n"),
    ("@ng", "Ang"),
    ("ang", "9ng"),
    ("al", "9l"),
    ("Ol", "0l"),
];

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PhonemizeOptions {
    pub max_moras: usize,
    pub voicebank_profile: String,
    pub reading_language: String,
    pub inventory_profile: String,
}
impl Default for PhonemizeOptions {
    fn default() -> Self {
        Self {
            max_moras: 240,
            voicebank_profile: "standard".into(),
            reading_language: "pt-BR".into(),
            inventory_profile: String::new(),
        }
    }
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct AliasResolution {
    pub alias: String,
    pub preutterance_ms: f64,
    pub overlap_ms: f64,
    pub consonant_ms: f64,
}
pub type AliasResolver<'a> = dyn Fn(&[String]) -> Option<AliasResolution> + 'a;
fn table<'a>(values: &[(&str, &'a str)], key: &str) -> Option<&'a str> {
    values.iter().find(|(a, _)| *a == key).map(|(_, b)| *b)
}
fn strings(xs: &[&str]) -> Vec<String> {
    xs.iter().map(|s| s.to_string()).collect()
}
fn unique(xs: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut out = vec![];
    for s in xs {
        let normalized = s.nfkc().collect::<String>().trim().to_string();
        if !normalized.is_empty() && seen.insert(normalized.clone()) {
            out.push(normalized);
        }
    }
    out
}
fn kana_aliases(kana: &str, extra: Vec<String>) -> Vec<String> {
    let primary = match kana {
        "じ" => strings(&["じ", "ぢ", "ji"]),
        "ず" => strings(&["ず", "づ", "zu"]),
        "し" => strings(&["し", "shi", "si"]),
        "ち" => strings(&["ち", "chi", "ti"]),
        "つ" => strings(&["つ", "tsu", "tu"]),
        "ふ" => strings(&["ふ", "fu", "hu"]),
        "を" => strings(&["を", "お", "wo"]),
        "ん" => strings(&["ん", "n"]),
        _ => vec![kana.into()],
    };
    unique(primary.into_iter().chain(extra))
}
fn cv_aliases(consonant: &str, vowel_phone: &str) -> Vec<String> {
    let v = match vowel_phone {
        "a~" => "a",
        "e~" | "E" => "e",
        "i~" => "i",
        "o~" | "O" => "o",
        "u~" => "u",
        s => s,
    };
    let v = if matches!(v, "a" | "e" | "i" | "o" | "u") {
        v
    } else {
        "a"
    };
    if consonant.is_empty() {
        return kana_aliases(table(ROMAJI_TO_KANA, v).unwrap(), vec![v.into()]);
    }
    match (consonant, v) {
        ("t", "u") => return strings(&["とぅ", "tu", "と", "to", "つ", "tsu"]),
        ("d", "u") => return strings(&["どぅ", "du", "ど", "do"]),
        ("s", "i") => return strings(&["すぃ", "si", "し", "shi"]),
        ("z", "i") => return strings(&["ずぃ", "zi", "じ", "ji"]),
        _ => {}
    }
    if consonant == "v" {
        let voiced = match v {
            "a" => "ゔぁ",
            "e" => "ゔぇ",
            "i" => "ゔぃ",
            "o" => "ゔぉ",
            _ => "ゔ",
        };
        let key = format!("b{v}");
        return unique(vec![
            voiced.into(),
            voiced.replace('ゔ', "ヴ"),
            format!("v{v}"),
            table(ROMAJI_TO_KANA, &key).unwrap().into(),
            key,
        ]);
    }
    if matches!(consonant, "L" | "J") {
        let close = match (consonant, v) {
            ("L", "a") => "りゃ",
            ("L", "e") => "りぇ",
            ("L", "i") => "り",
            ("L", "o") => "りょ",
            ("L", _) => "りゅ",
            ("J", "a") => "にゃ",
            ("J", "e") => "にぇ",
            ("J", "i") => "に",
            ("J", "o") => "にょ",
            _ => "にゅ",
        };
        let p = if consonant == "L" { "r" } else { "n" };
        let key = format!("{p}{v}");
        return unique(vec![
            close.into(),
            format!("{p}y{v}"),
            table(ROMAJI_TO_KANA, &key).unwrap().into(),
            key,
        ]);
    }
    let prefix = match consonant {
        "b" => "b",
        "d" => "d",
        "dZ" | "Z" => "j",
        "f" => "f",
        "g" => "g",
        "j" => "y",
        "k" => "k",
        "l" | "r" => "r",
        "m" => "m",
        "n" => "n",
        "p" => "p",
        "R" => "h",
        "s" => "s",
        "S" => "sh",
        "t" => "t",
        "tS" => "ch",
        "w" => "w",
        "z" => "z",
        _ => "",
    };
    let key = format!("{prefix}{v}");
    let Some(kana) = table(ROMAJI_TO_KANA, &key) else {
        return kana_aliases(table(ROMAJI_TO_KANA, v).unwrap(), vec![v.into()]);
    };
    let mut xs = vec![kana.into(), key];
    if consonant == "S" {
        let s = format!("s{v}");
        if let Some(kana) = table(ROMAJI_TO_KANA, &s) {
            xs.extend([kana.into(), s]);
        }
    } else if matches!(consonant, "Z" | "dZ") {
        let z = format!("z{v}");
        let d = format!("d{v}");
        if let Some(kana) = table(ROMAJI_TO_KANA, &z).or_else(|| table(ROMAJI_TO_KANA, &d)) {
            xs.push(kana.into());
        }
    } else if consonant == "R" {
        let r = format!("r{v}");
        if let Some(kana) = table(ROMAJI_TO_KANA, &r) {
            xs.extend([kana.into(), r]);
        }
    }
    unique(xs)
}
fn aux_aliases(phone: &str, role: &str) -> Vec<String> {
    if matches!(phone, "m" | "n") || role == "nasal" {
        return kana_aliases("ん", strings(&["n"]));
    }
    match phone {
        "j" => kana_aliases("い", strings(&["i"])),
        "w" | "l" => kana_aliases("う", strings(&["u"])),
        "s" => kana_aliases("す", strings(&["su"])),
        "S" => kana_aliases("し", strings(&["shi"])),
        "z" => kana_aliases("ず", strings(&["zu"])),
        "Z" => kana_aliases("じ", strings(&["ji"])),
        "r" | "R" => kana_aliases("る", strings(&["ru"])),
        _ => cv_aliases(
            phone,
            match phone {
                "d" | "t" => "o",
                "dZ" | "tS" => "i",
                _ => "u",
            },
        ),
    }
}
fn prefer_available(xs: Vec<String>, resolver: Option<&AliasResolver<'_>>) -> Vec<String> {
    if let Some(r) = resolver {
        if let Some(entry) = r(&xs) {
            if !entry.alias.trim().is_empty() {
                return unique(std::iter::once(entry.alias).chain(xs));
            }
        }
    }
    xs
}
fn mora(
    candidates: Vec<String>,
    duration: f64,
    role: &str,
    source: Vec<String>,
    stressed: bool,
    deaccented: bool,
    resolver: Option<&AliasResolver<'_>>,
) -> Mora {
    Mora {
        candidates: prefer_available(candidates, resolver),
        duration_ms: duration,
        role: role.into(),
        source_phonemes: source,
        stressed,
        deaccented,
        ..Mora::default()
    }
}
fn pt_moras(
    word: &PhoneticWord,
    deaccented: bool,
    resolver: Option<&AliasResolver<'_>>,
) -> Vec<Mora> {
    let mut out = vec![];
    for s in &word.syllables {
        for p in s.onset.iter().take(s.onset.len().saturating_sub(1)) {
            out.push(mora(
                aux_aliases(p, "epenthetic"),
                48.,
                "epenthetic",
                vec![p.clone()],
                false,
                true,
                resolver,
            ));
        }
        let onset = s.onset.last().map(String::as_str).unwrap_or("");
        let mut source = s.onset.last().cloned().into_iter().collect::<Vec<_>>();
        source.push(s.vowel.clone());
        out.push(mora(
            cv_aliases(onset, &s.vowel),
            135.,
            "nucleus",
            source,
            s.stressed && !deaccented,
            deaccented,
            resolver,
        ));
        if s.vowel.ends_with('~') {
            out.push(mora(
                aux_aliases("n", "nasal"),
                66.,
                "nasal",
                vec![s.vowel.clone()],
                false,
                true,
                resolver,
            ));
        }
        for p in &s.coda {
            let role = if glide(p) { "glide" } else { "coda" };
            out.push(mora(
                aux_aliases(p, role),
                if role == "glide" { 50. } else { 52. },
                role,
                vec![p.clone()],
                false,
                true,
                resolver,
            ));
        }
    }
    out
}
fn kana_moras(word: &str, deaccented: bool, resolver: Option<&AliasResolver<'_>>) -> Vec<Mora> {
    let mut raw: Vec<String> = vec![];
    for c in word.chars() {
        let cp = c as u32;
        let c = if (0x30a1..=0x30f6).contains(&cp) {
            char::from_u32(cp - 0x60).unwrap()
        } else {
            c
        };
        if "ゃゅょぁぃぅぇぉゎ".contains(c) && !raw.is_empty() {
            raw.last_mut().unwrap().push(c);
        } else if c == 'ー' && !raw.is_empty() {
            raw.push(raw.last().unwrap().chars().last().unwrap().to_string());
        } else if ('ぁ'..='ゖ').contains(&c) || c == 'ん' {
            raw.push(c.to_string());
        }
    }
    raw.into_iter()
        .enumerate()
        .map(|(i, kana)| {
            mora(
                kana_aliases(&kana, vec![]),
                if kana == "ん" { 150. } else { 135. },
                if kana == "ん" { "nasal" } else { "nucleus" },
                vec![kana],
                i == 0 && !deaccented,
                deaccented,
                resolver,
            )
        })
        .collect()
}
fn xsampa(phone: &str) -> &str {
    match phone {
        "a~" => "a",
        "e~" => "e",
        "i~" => "i",
        "o~" => "o",
        "u~" => "u",
        s => s.trim_end_matches('~'),
    }
}
fn diphthong(v: &str, g: &str) -> Option<&'static str> {
    match (v, g) {
        ("a", "j") => Some("aI"),
        ("e" | "E", "j") => Some("eI"),
        ("o" | "O", "j") => Some("OI"),
        ("a", "w") => Some("aU"),
        ("o" | "O", "w") => Some("oU"),
        _ => None,
    }
}
fn bank_vowel<'a>(p: &'a str, inventory: &str) -> &'a str {
    match inventory {
        "miku-v4-english" => table(MIKU_V4_VOWELS, p).unwrap_or(p),
        "anna-nyui-diva" => table(ANNA_DIVA_VOWELS, p).unwrap_or(p),
        _ => p,
    }
}
fn bank_consonant<'a>(p: &'a str, inventory: &str) -> &'a str {
    if inventory == "anna-nyui-diva" {
        table(ANNA_DIVA_CONSONANTS, p).unwrap_or(p)
    } else {
        p
    }
}
fn bank_cc(left: &str, right: &str, inventory: &str) -> Vec<String> {
    let mut xs = vec![format!("{left} {right}"), format!("{left}{right}")];
    if inventory == "anna-nyui-diva" {
        if let Some(s) = table(ANNA_DIVA_VC_EXCEPTIONS, &format!("{left}{right}")) {
            xs.insert(0, s.into());
        }
    }
    if inventory == "miku-v4-english" {
        let alt = match right {
            "t" => Some("th"),
            "k" => Some("kh"),
            "p" => Some("ph"),
            "b" => Some("bh"),
            "d" => Some("dh"),
            "g" => Some("gh"),
            "l" => Some("l0"),
            _ => None,
        };
        if let Some(alt) = alt {
            xs.push(format!("{left} {alt}"));
        }
    }
    xs
}
fn xsampa_onset(phones: &[String], dedicated: bool, inventory: &str) -> Vec<String> {
    let mut out = vec![];
    for p in phones {
        let ps = match p.as_str() {
            "J" if !dedicated => strings(&["n", "j"]),
            "L" if !dedicated => strings(&["l", "j"]),
            "R" => strings(&["h"]),
            _ => vec![p.clone()],
        };
        out.extend(ps.iter().map(|p| bank_consonant(p, inventory).to_string()));
    }
    out
}
pub fn english_vowel_source(phones: &[String]) -> Vec<String> {
    phones
        .iter()
        .rposition(|p| {
            vowel(p)
                || table(ANNA_DIVA_VOWELS, p).is_some()
                || matches!(p.as_str(), "aI" | "aU" | "eI" | "oU" | "OI")
        })
        .map(|i| phones[i..].to_vec())
        .unwrap_or_default()
}
pub fn english_vowel_candidates(phones: &[String], inventory: &str) -> Vec<String> {
    let xs = english_vowel_source(phones);
    let Some(first) = xs.first() else {
        return vec![];
    };
    let mut v = xsampa(first);
    if !first.ends_with('~') && xs.len() > 1 {
        v = diphthong(v, &xs[1]).unwrap_or(v);
    }
    vec![bank_vowel(v, inventory).into()]
}
pub fn pururu_vowel_candidates(phones: &[String]) -> Vec<String> {
    english_vowel_source(phones)
        .first()
        .map(|v| vec![format!("*{}", pururu_phone(v))])
        .unwrap_or_default()
}

/// Versioned Pururu speech coarticulation. Only authored VV/VC recordings
/// confirmed by the installed bank may replace a lexical boundary.
pub fn phonemize_pururu_profile(
    text: &str,
    options: &PhonemizeOptions,
    resolver: Option<&dyn Fn(&[String]) -> Option<AliasResolution>>,
) -> Result<Vec<Mora>, String> {
    let native = options.inventory_profile == "pururu-purpura";
    let cleaned = if native {
        text.replace('|', ".")
    } else {
        text.to_owned()
    };
    let planned = phonemize(&cleaned, options, resolver)?;
    let Some(resolve) = resolver.filter(|_| native) else {
        return Ok(planned);
    };
    let mut remaining = options.max_moras.max(1).saturating_sub(planned.len());
    let mut output: Vec<Mora> = Vec::new();
    let mut first = 0;
    while first < planned.len() {
        let word = planned[first].word_index;
        let end = planned[first..]
            .iter()
            .position(|m| m.word_index != word)
            .map(|n| first + n)
            .unwrap_or(planned.len());
        let mut current = planned[first..end].to_vec();
        let mut linked = false;
        if let (Some(last), Some(incoming)) =
            (output.last(), current.iter().find(|m| m.role == "nucleus"))
        {
            let source = english_vowel_source(&last.source_phonemes);
            if current.last().is_some_and(|m| m.word_end)
                && last.role == "nucleus"
                && last.phrase_end.is_empty()
                && last.pause_after_ms <= 6.0
                && source.len() == 1
                && !incoming.source_phonemes.is_empty()
            {
                let vowel = pururu_phone(&source[0]);
                let onset: Vec<&str> = incoming.source_phonemes
                    [..incoming.source_phonemes.len() - 1]
                    .iter()
                    .map(|p| pururu_phone(p))
                    .collect();
                let next = pururu_phone(incoming.source_phonemes.last().unwrap());
                let candidates = if let Some(consonant) = onset.first() {
                    vec![
                        format!("{vowel}{consonant}"),
                        format!("{vowel} {consonant}"),
                    ]
                } else {
                    vec![format!("{vowel} {next}")]
                };
                let found = candidates.iter().find_map(|c| resolve(&[c.clone()]));
                if let Some(found) = found {
                    let mut aliases = vec![found.alias];
                    for candidate in candidates {
                        if !aliases.contains(&candidate) {
                            aliases.push(candidate);
                        }
                    }
                    if onset.is_empty() {
                        let i = current.iter().position(|m| m.role == "nucleus").unwrap();
                        current[i].candidates = aliases;
                        linked = true;
                    } else if remaining > 0 && resolve(&incoming.candidates).is_some() {
                        let mut helper = incoming.clone();
                        helper.candidates = aliases;
                        helper.duration_ms = 58.0;
                        helper.pause_after_ms = 0.0;
                        helper.word_end = false;
                        helper.phrase_end.clear();
                        helper.stressed = false;
                        helper.deaccented = true;
                        helper.role = "vowel-exit".into();
                        helper.source_phonemes = vec![vowel.to_owned(), onset[0].to_owned()];
                        helper.coverage = "cvvc-transition".into();
                        helper.planner_cost = 0.0;
                        current.insert(0, helper);
                        remaining -= 1;
                        linked = true;
                    }
                }
            }
        }
        if linked {
            if let Some(last) = output.last_mut() {
                last.pause_after_ms = 0.0;
            }
            let total = current.len();
            for (i, mora) in current.iter_mut().enumerate() {
                mora.mora_index = i;
                mora.word_moras = total;
            }
        }
        output.extend(current);
        first = end;
    }
    Ok(output)
}
fn alias_score(kind: &str, alias: &str, entry: Option<&AliasResolution>, rank: usize) -> f64 {
    let mut score = match kind {
        "cvvc-direct" => 0.,
        "cluster-hit" => 8.,
        "cvvc-transition" => 14.,
        "approximation" => 42.,
        _ => 48.,
    } + rank as f64 * 1.5;
    if alias.starts_with("- ") {
        score -= 2.5;
    }
    if alias.contains(' ') {
        score -= 2.;
    }
    if alias.replace(['-', ' '], "").chars().count() >= 3 {
        score -= 1.;
    }
    if let Some(e) = entry {
        let (pre, over, con) = (
            e.preutterance_ms.max(0.),
            e.overlap_ms.max(0.),
            e.consonant_ms.max(0.),
        );
        if (alias.starts_with("- ") || alias.contains(' ')) && pre <= 0. {
            score += 5.;
        }
        if pre > 0. && over <= 0. {
            score += 2.;
        }
        if over >= 4. && over <= pre + 8. {
            score -= 1.5;
        }
        if con > 0. {
            score -= 0.5;
        }
        if pre > 220. {
            score += ((pre - 220.) / 25.).min(12.);
        }
    }
    (score.max(0.) * 1000.).round_ties_even() / 1000.
}
type Tiers = Vec<(&'static str, Vec<String>)>;
fn english_resolve(
    tiers: Tiers,
    resolver: Option<&AliasResolver<'_>>,
) -> (Vec<String>, String, f64) {
    if resolver.is_none() {
        for (kind, xs) in tiers {
            let xs = unique(xs);
            if !xs.is_empty() {
                let cost = alias_score(kind, &xs[0], None, 0);
                return (xs, kind.into(), cost);
            }
        }
        return (vec![], "approximation".into(), 99.);
    }
    let resolver = resolver.unwrap();
    let mut first = vec![];
    let mut flattened = vec![];
    let mut found: Option<(f64, usize, String, String)> = None;
    let mut order = 0;
    for (kind, xs) in tiers {
        let xs = unique(xs);
        if xs.is_empty() {
            continue;
        }
        if first.is_empty() {
            first = xs.clone();
        }
        for (rank, candidate) in xs.into_iter().enumerate() {
            flattened.push(candidate.clone());
            if let Some(entry) = resolver(std::slice::from_ref(&candidate)) {
                let alias = if entry.alias.trim().is_empty() {
                    candidate
                } else {
                    entry.alias.trim().into()
                };
                let score = alias_score(kind, &alias, Some(&entry), rank);
                if found
                    .as_ref()
                    .is_none_or(|old| score < old.0 || (score == old.0 && order < old.1))
                {
                    found = Some((score, order, kind.into(), alias));
                }
            }
            order += 1;
        }
    }
    if let Some((score, _, kind, alias)) = found {
        (unique(std::iter::once(alias).chain(flattened)), kind, score)
    } else {
        (first, "approximation".into(), 99.)
    }
}
fn english_mora(
    tiers: Tiers,
    duration: f64,
    role: &str,
    source: Vec<String>,
    stressed: bool,
    deaccented: bool,
    resolver: Option<&AliasResolver<'_>>,
) -> Mora {
    let (candidates, coverage, planner_cost) = english_resolve(tiers, resolver);
    Mora {
        candidates,
        duration_ms: duration,
        role: role.into(),
        source_phonemes: source,
        stressed,
        deaccented,
        coverage,
        planner_cost,
        profile: "english-cvvc".into(),
        ..Mora::default()
    }
}
fn nucleus_candidates(
    onset: &[String],
    v: &str,
    previous: &str,
    start: bool,
    inventory: &str,
) -> Tiers {
    let cluster = onset.join("");
    let last = onset.last().map(String::as_str).unwrap_or("");
    let mut tiers = vec![];
    if start {
        if !cluster.is_empty() {
            if inventory == "anna-nyui-diva" && cluster == "h" && v == "E" {
                tiers.push(("cvvc-direct", strings(&["-hhE"])));
            }
            tiers.push((
                "cvvc-direct",
                vec![if inventory == "anna-nyui-diva" {
                    format!("-{cluster}{v}")
                } else {
                    format!("- {cluster}{v}")
                }],
            ));
            if last == "r" {
                tiers.push((
                    "cvvc-direct",
                    vec![format!("- {}4{v}", cluster.trim_end_matches('r'))],
                ));
            }
        } else {
            tiers.push((
                "cvvc-direct",
                vec![if inventory == "anna-nyui-diva" {
                    format!("-{v}")
                } else {
                    format!("- {v}")
                }],
            ));
        }
        if inventory == "miku-v4-english" && cluster.is_empty() {
            tiers.push(("cvvc-direct", vec![format!("Sil {v}")]));
        }
    } else if !previous.is_empty() {
        tiers.push((
            "cvvc-direct",
            if !cluster.is_empty() {
                vec![
                    format!("{previous} {cluster}{v}"),
                    format!("{previous}{cluster} {v}"),
                ]
            } else {
                vec![format!("{previous} {v}"), format!("{previous}{v}")]
            },
        ));
    }
    if !cluster.is_empty() {
        let mut xs = vec![format!("{cluster}{v}")];
        if inventory == "anna-nyui-diva" && cluster == "h" && v == "E" {
            xs.insert(0, "hhE".into());
        }
        if inventory == "anna-nyui-diva" && onset.len() > 1 {
            xs.push(format!("_{last}{v}"));
        }
        xs.extend([format!("{last}{v}"), format!("{last} {v}")]);
        if last == "r" {
            xs.extend([format!("4{v}"), format!("4 {v}")]);
        }
        tiers.push(("cvvc-direct", xs));
    } else {
        tiers.push(("cvvc-direct", vec![v.into()]));
    }
    tiers
}
fn english_duration(
    s: &Syllable,
    deaccented: bool,
    index: usize,
    count: usize,
    diphthong: bool,
) -> f64 {
    let mut n: f64 = 108.;
    if deaccented {
        n -= 16.;
    } else if s.stressed {
        n += 14.;
    }
    if s.onset.len() >= 2 {
        n += 4.;
    }
    if !s.coda.is_empty() {
        n += 4.;
    }
    if diphthong {
        n += 5.;
    }
    if index == count.saturating_sub(1) {
        n += 4.;
    }
    n.round_ties_even().clamp(82., 142.)
}
fn cluster_units(
    onset: &[String],
    start: bool,
    previous: &str,
    resolver: Option<&AliasResolver<'_>>,
    compositional_palatal: bool,
    inventory: &str,
) -> Vec<Mora> {
    if onset.is_empty() {
        return vec![];
    }
    let mut units = vec![];
    let cluster = onset.join("");
    let first = &onset[0];
    let mut initial_pairs = vec![];
    if start && onset.len() > 1 {
        if inventory == "miku-v4-english" {
            initial_pairs = bank_cc(&onset[0], &onset[1], inventory);
        }
        let primary = if inventory == "anna-nyui-diva" {
            vec![format!("-{cluster}"), cluster.clone()]
        } else {
            std::iter::once(format!("- {cluster}"))
                .chain(initial_pairs.clone())
                .collect()
        };
        let fallback = if inventory == "miku-v4-english" {
            vec![format!("- {first}"), format!("Sil {first}"), first.clone()]
        } else {
            vec![format!("- {first}"), first.clone()]
        };
        let approx = if inventory == "miku-v4-english" {
            vec![format!("@ {first}"), format!("e {first}")]
        } else {
            vec![]
        };
        units.push(english_mora(
            vec![
                ("cluster-hit", primary),
                ("cvvc-transition", fallback),
                ("approximation", approx),
            ],
            54.,
            "cluster",
            onset.to_vec(),
            false,
            true,
            resolver,
        ));
        if inventory == "miku-v4-english" && !units[0].candidates.is_empty() {
            let selected = &units[0].candidates[0];
            if initial_pairs
                .iter()
                .any(|a| selected == a || selected.starts_with(&format!("{a}_")))
            {
                units[0].source_phonemes = onset[..2].to_vec();
            } else if !selected.replace(' ', "").contains(&cluster) {
                units[0].source_phonemes = vec![first.clone()];
            }
        }
    } else if start {
        units.push(english_mora(
            vec![("cvvc-transition", vec![format!("- {first}"), first.clone()])],
            58.,
            "transition",
            vec![first.clone()],
            false,
            true,
            resolver,
        ));
    } else if !previous.is_empty() {
        let prefix2 = onset.iter().take(2).cloned().collect::<String>();
        let fallbacks = if onset.len() > 1 {
            vec![
                if inventory == "anna-nyui-diva" {
                    format!("-{cluster}")
                } else {
                    format!("- {cluster}")
                },
                cluster.clone(),
            ]
        } else {
            vec![]
        };
        let full_fallback =
            !fallbacks.is_empty() && resolver.is_some_and(|r| r(&fallbacks).is_some());
        let mut fallback = fallbacks;
        if !full_fallback {
            fallback.extend([format!("- {first}"), first.clone()]);
        }
        let tiers = vec![
            (
                if onset.len() > 1 {
                    "cluster-hit"
                } else {
                    "cvvc-transition"
                },
                if onset.len() > 1 {
                    vec![format!("{previous} {prefix2}")]
                } else {
                    vec![]
                },
            ),
            (
                "cvvc-transition",
                vec![format!("{previous} {first}"), format!("{previous}{first}")],
            ),
            ("approximation", fallback),
        ];
        units.push(english_mora(
            tiers,
            58.,
            "transition",
            vec![previous.into(), first.clone()],
            false,
            true,
            resolver,
        ));
    }
    let mut covered = false;
    if let Some(unit) = units.first_mut() {
        if let Some(alias) = unit.candidates.first() {
            let compact = alias.replace(['-', ' '], "");
            let renderable = resolver.is_none_or(|r| r(std::slice::from_ref(alias)).is_some());
            covered = renderable && compact.contains(&cluster);
            if covered && unit.coverage == "approximation" {
                unit.role = "cluster".into();
                unit.source_phonemes = onset.to_vec();
            }
        }
    }
    if onset.len() > 1 && !covered {
        for (number, pair) in onset.windows(2).enumerate() {
            let (left, right) = (&pair[0], &pair[1]);
            let mut aliases = bank_cc(left, right, inventory);
            let mut kind = "cluster-hit";
            let mut source = vec![left.clone(), right.clone()];
            if inventory == "miku-v4-english" && start && number == 0 && !units.is_empty() {
                if let Some(selected) = units[0].candidates.first() {
                    if initial_pairs
                        .iter()
                        .any(|a| selected == a || selected.starts_with(&format!("{a}_")))
                    {
                        continue;
                    }
                }
            }
            if inventory == "miku-v4-english" {
                if let Some(r) = resolver {
                    if r(&aliases).is_none() {
                        if number == 0 && !units.is_empty() && r(&units[0].candidates).is_some() {
                            continue;
                        }
                        aliases = vec![format!("@ {left}"), format!("e {left}")];
                        kind = "approximation";
                        source = vec![left.clone()];
                    }
                }
            }
            let special = matches!(
                onset
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .as_slice(),
                ["n", "j"] | ["l", "j"] | ["n", "y"] | ["l", "y"]
            );
            if compositional_palatal
                && !start
                && !previous.is_empty()
                && special
                && !units.is_empty()
                && units[0].coverage == "cvvc-transition"
            {
                if let Some(r) = resolver {
                    if units[0]
                        .candidates
                        .first()
                        .is_some_and(|a| r(std::slice::from_ref(a)).is_some())
                        && r(&aliases).is_none()
                    {
                        continue;
                    }
                }
            }
            units.push(english_mora(
                vec![
                    (kind, aliases),
                    ("approximation", vec![left.clone(), right.clone()]),
                ],
                54.,
                "cluster",
                source,
                false,
                true,
                resolver,
            ));
        }
    }
    units
}
fn coda_units(
    v: &str,
    coda: &[String],
    nasal: bool,
    resolver: Option<&AliasResolver<'_>>,
    inventory: &str,
) -> Vec<Mora> {
    let mut out = vec![];
    let mut previous = v.to_string();
    let mut phones = if nasal { vec!["N".to_string()] } else { vec![] };
    for p in coda {
        phones.extend(xsampa_onset(std::slice::from_ref(p), false, ""));
    }
    for raw in phones {
        let phone = if raw == "r" && inventory != "miku-v4-english" {
            "4"
        } else {
            &raw
        };
        let phone = bank_consonant(phone, inventory).to_string();
        let role = if raw == "N" {
            "nasal"
        } else if glide(&raw) {
            "glide"
        } else {
            "coda"
        };
        let mut contextual = bank_cc(&previous, &phone, inventory);
        if raw == "r" {
            contextual.extend([format!("{previous} {raw}"), format!("{previous}{raw}")]);
        }
        let mut fallback = vec![phone.clone(), raw.clone()];
        if raw == "N" && inventory == "anna-nyui-diva" {
            fallback.splice(0..0, [format!("{v}n"), format!("{v} n")]);
        }
        if previous == bank_consonant("N", inventory)
            && role == "glide"
            && resolver.is_some_and(|r| r(&unique(fallback.clone())).is_none())
        {
            fallback.extend([format!("{v} {phone}"), format!("{v}{phone}")]);
        }
        out.push(english_mora(
            vec![("cvvc-transition", contextual), ("approximation", fallback)],
            if role == "nasal" {
                66.
            } else if role == "glide" {
                54.
            } else {
                58.
            },
            role,
            vec![raw],
            false,
            true,
            resolver,
        ));
        previous = phone;
    }
    out
}
fn english_moras(
    word: &PhoneticWord,
    deaccented: bool,
    resolver: Option<&AliasResolver<'_>>,
    inventory: &str,
) -> Vec<Mora> {
    let mut out = vec![];
    let mut previous = String::new();
    let count = word.syllables.len().max(1);
    for (index, s) in word.syllables.iter().enumerate() {
        let fallback_onset = xsampa_onset(&s.onset, false, inventory);
        let dedicated = xsampa_onset(&s.onset, true, inventory);
        let canonical = xsampa(&s.vowel);
        let base = bank_vowel(canonical, inventory).to_string();
        let mut coda = s.coda.clone();
        let gl = coda
            .first()
            .filter(|p| glide(p))
            .cloned()
            .unwrap_or_default();
        let nasal = s.vowel.ends_with('~');
        let start = index == 0;
        let mut vowels: Vec<(String, bool, bool)> = vec![];
        if nasal {
            if !gl.is_empty() {
                vowels.push((format!("{}{gl}", s.vowel), true, false));
            }
            vowels.push((s.vowel.clone(), false, false));
        } else if !gl.is_empty() {
            if let Some(d) = diphthong(canonical, &gl) {
                vowels.push((bank_vowel(d, inventory).into(), true, false));
            }
        }
        vowels.push((base.clone(), false, nasal));
        let mut onsets = vec![];
        if dedicated != fallback_onset {
            onsets.push(dedicated.clone());
        }
        onsets.push(fallback_onset.clone());
        let mut selected = None;
        if let Some(r) = resolver {
            'outer: for (v, absorbs, tail) in &vowels {
                for onset in &onsets {
                    let resolved = english_resolve(
                        nucleus_candidates(onset, v, &previous, start, inventory),
                        resolver,
                    );
                    if resolved
                        .0
                        .first()
                        .is_some_and(|a| r(std::slice::from_ref(a)).is_some())
                    {
                        selected = Some((onset.clone(), v.clone(), *absorbs, *tail, resolved));
                        break 'outer;
                    }
                }
            }
        }
        let (onset, v, absorbs, tail, (candidates, mut coverage, cost)) =
            selected.unwrap_or_else(|| {
                (
                    fallback_onset.clone(),
                    base.clone(),
                    false,
                    nasal,
                    english_resolve(
                        nucleus_candidates(&fallback_onset, &base, &previous, start, inventory),
                        resolver,
                    ),
                )
            });
        if tail || (dedicated != fallback_onset && onset == fallback_onset) {
            coverage = "approximation".into();
        }
        if inventory == "miku-v4-english"
            && v == base
            && matches!(canonical, "a" | "A" | "o" | "O" | "E")
            && canonical != base
        {
            coverage = "approximation".into();
        }
        if absorbs && !coda.is_empty() {
            coda.remove(0);
        }
        let alias = candidates.first().cloned().unwrap_or_default();
        let compact = alias.replace(['-', ' '], "");
        let onset_compact = onset.join("");
        let encoded = !onset.is_empty()
            && compact.contains(&onset_compact)
            && (alias.starts_with("- ")
                || alias.contains(' ')
                || compact.starts_with(&onset_compact));
        let mut units = vec![];
        if encoded
            && index > 0
            && !word.syllables[index - 1].vowel.ends_with('~')
            && word.syllables[index - 1].coda.is_empty()
            && !previous.is_empty()
            && onset.len() == 1
        {
            if let Some(r) = resolver {
                let consonant = &onset[0];
                let native = if inventory == "anna-nyui-diva" && consonant == "h" && v == "E" {
                    "hh"
                } else {
                    consonant
                };
                if alias.starts_with(&format!("{consonant}{v}"))
                    || alias.starts_with(&format!("{consonant} {v}"))
                    || alias.starts_with(&format!("{native}{v}"))
                {
                    let exits = bank_cc(&previous, consonant, inventory);
                    if r(&exits).is_some() {
                        units.push(english_mora(
                            vec![("cvvc-transition", exits)],
                            58.,
                            "vowel-exit",
                            vec![previous.clone(), consonant.clone()],
                            false,
                            true,
                            resolver,
                        ));
                    }
                }
            }
        }
        if !onset.is_empty() && !encoded {
            let palatal = matches!(
                s.onset
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .as_slice(),
                ["J"] | ["L"]
            ) && onset == fallback_onset
                && compact.ends_with(&format!("{}{v}", onset.last().unwrap()));
            units.extend(cluster_units(
                &onset, start, &previous, resolver, palatal, inventory,
            ));
        }
        let mut source = s.onset.clone();
        source.push(s.vowel.clone());
        if absorbs {
            source.extend(s.coda.first().cloned());
        }
        units.push(Mora {
            candidates,
            duration_ms: english_duration(
                s,
                deaccented,
                index,
                count,
                !gl.is_empty() || matches!(s.vowel.as_str(), "aI" | "aU" | "eI" | "oU" | "OI"),
            ),
            stressed: s.stressed && !deaccented,
            deaccented,
            role: "nucleus".into(),
            source_phonemes: source,
            coverage,
            profile: "english-cvvc".into(),
            planner_cost: cost,
            ..Mora::default()
        });
        units.extend(coda_units(&v, &coda, tail, resolver, inventory));
        for mut unit in units {
            unit.profile = "english-cvvc".into();
            unit.syllable_index = index;
            unit.word_syllables = count;
            out.push(unit);
        }
        previous = v;
    }
    out
}
fn pururu_phone(p: &str) -> &str {
    match p {
        "a~" => "6~",
        "r" => "4",
        _ => p,
    }
}
fn pururu_moras(
    word: &PhoneticWord,
    deaccented: bool,
    resolver: Option<&AliasResolver<'_>>,
) -> Vec<Mora> {
    let mut out = vec![];
    let mut previous = String::new();
    let count = word.syllables.len().max(1);
    for (index, s) in word.syllables.iter().enumerate() {
        let onset = s
            .onset
            .iter()
            .map(|p| pururu_phone(p).to_string())
            .collect::<Vec<_>>();
        let v = pururu_phone(&s.vowel).to_string();
        let unit = |xs: Vec<String>, role: &str, source: Vec<String>, duration: f64| {
            let mut m = english_mora(
                vec![(
                    if role == "nucleus" {
                        "cvvc-direct"
                    } else {
                        "cvvc-transition"
                    },
                    xs,
                )],
                duration,
                role,
                source,
                s.stressed && !deaccented && role == "nucleus",
                deaccented || role != "nucleus",
                resolver,
            );
            m.syllable_index = index;
            m.word_syllables = count;
            m
        };
        let candidates = if !onset.is_empty() {
            let full = format!("{}{v}", onset.join(""));
            let available = resolver.is_none_or(|r| r(std::slice::from_ref(&full)).is_some());
            let xs = if available {
                vec![full]
            } else {
                vec![format!("{}{v}", onset.last().unwrap())]
            };
            if !previous.is_empty() {
                let exits = vec![
                    format!("{previous}{}", onset[0]),
                    format!("{previous} {}", onset[0]),
                ];
                if resolver.is_none_or(|r| r(&exits).is_some()) {
                    out.push(unit(
                        exits,
                        "vowel-exit",
                        vec![previous.clone(), onset[0].clone()],
                        58.,
                    ));
                }
            }
            if !available {
                for pair in onset.windows(2) {
                    out.push(unit(
                        vec![
                            format!("{} {}", pair[0], pair[1]),
                            format!("{}{}", pair[0], pair[1]),
                        ],
                        "cluster",
                        pair.to_vec(),
                        54.,
                    ));
                }
            }
            xs
        } else if !previous.is_empty() {
            vec![format!("{previous} {v}"), format!("*{v}")]
        } else {
            vec![format!("-{v}"), format!("*{v}")]
        };
        let mut source = s.onset.clone();
        source.push(s.vowel.clone());
        out.push(unit(
            candidates,
            "nucleus",
            source,
            english_duration(
                s,
                deaccented,
                index,
                count,
                s.coda.first().is_some_and(|p| glide(p)),
            ),
        ));
        previous = v.clone();
        let mut last = v.clone();
        for raw in &s.coda {
            let phone = pururu_phone(raw);
            let tails = unique(vec![
                format!("{previous}{phone}"),
                format!("{last} {phone}"),
                format!("{last}{phone}"),
                format!("{v}{phone}"),
            ]);
            let role = if glide(raw) { "glide" } else { "coda" };
            out.push(unit(
                tails,
                role,
                vec![raw.clone()],
                if role == "glide" { 54. } else { 58. },
            ));
            previous.push_str(phone);
            last = phone.into();
        }
    }
    out
}
fn function_word(token: &str, english: bool) -> bool {
    let low = token.to_lowercase();
    if english {
        return [
            "a", "an", "the", "and", "or", "but", "of", "to", "for", "in", "on", "at", "by",
            "with", "as", "from", "than", "that", "is", "are", "was", "were", "has", "have", "had",
            "be", "been", "do", "does", "did", "can", "could", "would", "should", "will",
        ]
        .contains(&low.as_str());
    }
    if low.chars().any(explicit) {
        return false;
    }
    [
        "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "lhe", "lhes",
        "me", "na", "nas", "no", "nos", "o", "os", "para", "por", "que", "se", "sem", "te", "um",
        "uma", "umas", "uns",
    ]
    .contains(&plain(&low).as_str())
}
fn kana_char(c: char) -> bool {
    ('ぁ'..='ゖ').contains(&c) || ('ァ'..='ヺ').contains(&c)
}
pub fn phonemize(
    text: &str,
    options: &PhonemizeOptions,
    resolver: Option<&AliasResolver<'_>>,
) -> Result<Vec<Mora>, String> {
    let language = options.reading_language.trim().to_lowercase();
    let language = if language.is_empty() {
        "pt-br"
    } else {
        &language
    };
    if !matches!(language, "pt-br" | "pt" | "en-us" | "en") {
        return Err("idioma TTV deve ser pt-BR ou en-US".into());
    }
    let english = matches!(language, "en-us" | "en");
    let profile_english = options
        .voicebank_profile
        .to_lowercase()
        .starts_with("english");
    if options.inventory_profile == "pururu-purpura" && english {
        return Err("Pururu Púrpura oferece leitura em português brasileiro".into());
    }
    if english && !profile_english {
        return Err("a leitura em inglês requer a voicebank English-CVVC".into());
    }
    let mut normalized = text.nfkc().collect::<String>().trim().to_string();
    if normalized.is_empty() {
        return Ok(vec![]);
    }
    if options.inventory_profile == "pururu-purpura"
        && normalized.chars().any(|c| kana_char(c) || c == 'ー')
    {
        return Err("Pururu Púrpura oferece leitura em português brasileiro".into());
    }
    if english {
        normalized = expand_english_numbers(&normalized);
    }
    static PT_TOKENS: OnceLock<regex::Regex> = OnceLock::new();
    static EN_TOKENS: OnceLock<regex::Regex> = OnceLock::new();
    let tokenizer = if english {
        EN_TOKENS.get_or_init(|| {
            regex::Regex::new(r"[A-Za-zÀ-ÿ]+(?:['’][A-Za-zÀ-ÿ]+)*|[ぁ-ゖァ-ヺー]+|[,.!?:;\n]")
                .unwrap()
        })
    } else {
        PT_TOKENS
            .get_or_init(|| regex::Regex::new(r"[ぁ-ゖァ-ヺーA-Za-zÀ-ÿÇç]+|[,.!?:;\n]").unwrap())
    };
    let mut out: Vec<Mora> = vec![];
    let mut word_index = 0;
    for m in tokenizer.find_iter(&normalized) {
        let token = m.as_str();
        let pause = match token {
            "," => Some(85.),
            ";" => Some(135.),
            ":" => Some(115.),
            "." => Some(180.),
            "!" => Some(155.),
            "?" => Some(180.),
            "\n" => Some(190.),
            _ => None,
        };
        if let Some(pause) = pause {
            if let Some(last) = out.last_mut() {
                last.pause_after_ms = last.pause_after_ms.max(pause);
                last.phrase_end = token.into();
            }
            continue;
        }
        let deaccented = function_word(token, english);
        let word_moras = if token.chars().any(kana_char) {
            kana_moras(token, deaccented, resolver)
        } else {
            let word = if english {
                g2p_english_word(token, deaccented)
            } else {
                g2p_word(token, deaccented)
            };
            if options.inventory_profile == "pururu-purpura" {
                pururu_moras(&word, deaccented, resolver)
            } else if profile_english {
                english_moras(&word, deaccented, resolver, &options.inventory_profile)
            } else {
                pt_moras(&word, deaccented, resolver)
            }
        };
        let count = word_moras.len().max(1);
        let nonempty = !word_moras.is_empty();
        for (i, mut mora) in word_moras.into_iter().enumerate() {
            mora.word_index = word_index;
            mora.mora_index = i;
            mora.word_moras = count;
            out.push(mora);
            if out.len() >= options.max_moras.max(1) {
                return Ok(out);
            }
        }
        if nonempty {
            let last = out.last_mut().unwrap();
            last.pause_after_ms = last.pause_after_ms.max(6.);
            last.word_end = true;
            word_index += 1;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod phonemizer_tests {
    use super::*;
    #[derive(Deserialize)]
    struct Contract {
        text: String,
        options: PhonemizeOptions,
        aliases: Option<Vec<AliasResolution>>,
        expected: Vec<Mora>,
    }
    #[test]
    fn production_all_voicebank_contracts() {
        let cases: Vec<Contract> =
            serde_json::from_str(include_str!("../data/phonemizer-contracts.json")).unwrap();
        for (case_index, case) in cases.into_iter().enumerate() {
            let resolve = |candidates: &[String]| {
                case.aliases.as_ref().and_then(|aliases| {
                    candidates.iter().find_map(|candidate| {
                        aliases.iter().find(|e| e.alias == *candidate).cloned()
                    })
                })
            };
            let resolver = if case.aliases.is_some() {
                Some(&resolve as &AliasResolver<'_>)
            } else {
                None
            };
            let actual = phonemize(&case.text, &case.options, resolver).unwrap();
            assert_eq!(
                actual, case.expected,
                "case {case_index}: {:?}, {} {}",
                case.text, case.options.voicebank_profile, case.options.inventory_profile
            );
        }
    }
    #[test]
    fn language_errors_do_not_resolve_other_inventory() {
        let bad =
            |_: &[String]| -> Option<AliasResolution> { panic!("must reject before resolving") };
        for text in ["hello", "か", "カ", "ｶ"] {
            let options = PhonemizeOptions {
                inventory_profile: "pururu-purpura".into(),
                reading_language: if text == "hello" {
                    "en-US".into()
                } else {
                    "pt-BR".into()
                },
                ..PhonemizeOptions::default()
            };
            assert!(phonemize(text, &options, Some(&bad)).is_err());
        }
    }
    #[test]
    fn steady_vowels_keep_nasal_and_lexical_identity() {
        assert_eq!(
            english_vowel_candidates(&strings(&["h", "i"]), "anna-nyui-diva"),
            strings(&["E"])
        );
        assert_eq!(
            english_vowel_candidates(&strings(&["a~", "w"]), "anna-nyui-diva"),
            strings(&["a"])
        );
        assert_eq!(
            pururu_vowel_candidates(&strings(&["p", "a~"])),
            strings(&["*6~"])
        );
    }
}
