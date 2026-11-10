//! OTO and prefix.map parsing shared by all installed UTAU voices.
use encoding_rs::SHIFT_JIS;
use regex::Regex;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};
use unicode_normalization::UnicodeNormalization;
use walkdir::WalkDir;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OtoEntry {
    pub alias: String,
    pub wav_path: PathBuf,
    pub offset_ms: f64,
    pub consonant_ms: f64,
    pub cutoff_ms: f64,
    pub preutterance_ms: f64,
    pub overlap_ms: f64,
}
impl OtoEntry {
    pub fn cache_identity(&self) -> String {
        let stamp = fs::metadata(&self.wav_path)
            .map(|m| format!("{}:{}", m.len(), mtime_ns(&m)))
            .unwrap_or_else(|_| "missing".into());
        format!(
            "{}|{}|{}|{:.3}|{:.3}|{:.3}|{:.3}|{:.3}",
            self.alias,
            self.wav_path.display(),
            stamp,
            self.offset_ms,
            self.consonant_ms,
            self.cutoff_ms,
            self.preutterance_ms,
            self.overlap_ms
        )
    }
}
fn mtime_ns(meta: &fs::Metadata) -> u128 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}
fn normalized(s: &str) -> String {
    s.nfkc().collect::<String>().trim().to_owned()
}
fn folded(s: &str) -> String {
    normalized(s).to_lowercase().replace('ß', "ss")
}
fn decode(path: &Path) -> Result<String, String> {
    let raw = fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if let Ok(s) = std::str::from_utf8(&raw) {
        return Ok(s.trim_start_matches('\u{feff}').to_owned());
    }
    let (s, _, _) = SHIFT_JIS.decode(&raw);
    Ok(s.into_owned())
}
fn numeric(s: Option<&&str>) -> f64 {
    s.and_then(|s| s.trim().replace(',', ".").parse::<f64>().ok())
        .filter(|x| x.is_finite())
        .unwrap_or(0.0)
}
pub fn pitch_number(value: &str) -> Option<i32> {
    let re = Regex::new(r"^([A-Ga-g])([#b]?)(-?[0-9]{1,2})$").unwrap();
    let c = re.captures(value.trim())?;
    let step = match c[1].to_ascii_uppercase().as_str() {
        "C" => 0,
        "D" => 2,
        "E" => 4,
        "F" => 5,
        "G" => 7,
        "A" => 9,
        "B" => 11,
        _ => return None,
    };
    let shift = match &c[2] {
        "#" => 1,
        "b" => -1,
        _ => 0,
    };
    let num = (c[3].parse::<i32>().ok()? + 1) * 12 + step + shift;
    (0..=127).contains(&num).then_some(num)
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VoicebankOptions {
    pub minimum_aliases: usize,
    pub alias_suffix: String,
    pub case_sensitive: bool,
    pub pitch_map: bool,
}
impl Default for VoicebankOptions {
    fn default() -> Self {
        Self {
            minimum_aliases: 10,
            alias_suffix: String::new(),
            case_sensitive: false,
            pitch_map: false,
        }
    }
}
#[derive(Debug, Clone)]
pub struct VoicebankIndex {
    pub root: PathBuf,
    pub entries: BTreeMap<String, OtoEntry>,
    folded_entries: HashMap<String, OtoEntry>,
    pub fingerprint: String,
    pub name: String,
    pub pitch_map: BTreeMap<i32, (String, String)>,
}
impl VoicebankIndex {
    pub fn load(root: &Path, options: &VoicebankOptions) -> Result<Self, String> {
        let base = fs::canonicalize(root).map_err(|e| format!("voicebank não encontrada: {e}"))?;
        if !base.is_dir() {
            return Err("voicebank não encontrada".into());
        }
        let mut oto = Vec::new();
        let mut maps = Vec::new();
        let mut characters = Vec::new();
        for item in WalkDir::new(&base).follow_links(false) {
            let item = item.map_err(|e| e.to_string())?;
            if item.file_type().is_file() {
                match item.file_name().to_str() {
                    Some("oto.ini") => oto.push(item.path().to_owned()),
                    Some("prefix.map") => maps.push(item.path().to_owned()),
                    Some("character.txt") => characters.push(item.path().to_owned()),
                    _ => (),
                }
            }
        }
        oto.sort();
        maps.sort();
        characters.sort();
        if oto.is_empty() {
            return Err("voicebank sem oto.ini".into());
        }
        let mut digest = Sha256::new();
        digest.update(base.to_string_lossy().as_bytes());
        let mut suffix = normalized(&options.alias_suffix);
        if !suffix.is_empty() && !suffix.starts_with('_') {
            suffix.insert(0, '_');
        }
        if !suffix.is_empty() || options.case_sensitive {
            digest.update(
                format!(
                    "alias-map-v1:{suffix}:{}",
                    usize::from(options.case_sensitive)
                )
                .as_bytes(),
            );
        }
        let mut pitch_map = BTreeMap::new();
        if options.pitch_map {
            let root_map = base.join("prefix.map");
            let path = if root_map.is_file() {
                root_map
            } else {
                if maps.len() > 1 {
                    return Err(
                        "voicebank contém vários prefix.map; selecione a pasta do banco".into(),
                    );
                }
                maps.first()
                    .cloned()
                    .ok_or("voicebank multipitch sem prefix.map")?
            };
            let bytes = fs::read(&path).map_err(|e| e.to_string())?;
            digest.update(b"pitch-map-v1\0");
            digest.update(
                path.strip_prefix(&base)
                    .unwrap()
                    .to_string_lossy()
                    .as_bytes(),
            );
            digest.update(bytes);
            for line in decode(&path)?.lines() {
                let f: Vec<_> = line.trim_start_matches('\u{feff}').split('\t').collect();
                if f.len() < 3 {
                    continue;
                }
                if let Some(n) = pitch_number(f[0]) {
                    let prefix: String = f[1].nfkc().collect();
                    let suffix: String = f[2].nfkc().collect();
                    if [&prefix, &suffix]
                        .iter()
                        .any(|s| s.contains('\0') || s.chars().count() > 120)
                    {
                        return Err("prefix.map contém um prefixo ou sufixo inválido".into());
                    }
                    pitch_map.insert(n, (prefix, suffix));
                }
            }
            if pitch_map.is_empty() {
                return Err("prefix.map sem notas válidas".into());
            }
        }
        let mut entries = BTreeMap::new();
        let mut fold: HashMap<String, Option<OtoEntry>> = HashMap::new();
        for oto_path in oto {
            let canonical = fs::canonicalize(&oto_path).map_err(|e| e.to_string())?;
            if !canonical.starts_with(&base) {
                continue;
            }
            let text = decode(&oto_path)?;
            let stat = fs::metadata(&oto_path).map_err(|e| e.to_string())?;
            digest.update(
                oto_path
                    .strip_prefix(&base)
                    .unwrap()
                    .to_string_lossy()
                    .as_bytes(),
            );
            digest.update(format!("{}:{}", stat.len(), mtime_ns(&stat)).as_bytes());
            for line in text.lines() {
                let clean = line.trim().trim_start_matches('\u{feff}');
                if clean.is_empty() || clean.starts_with(['#', ';']) {
                    continue;
                }
                let Some((wav, payload)) = clean.split_once('=') else {
                    continue;
                };
                let f: Vec<_> = payload.split(',').map(str::trim).collect();
                let alias = normalized(
                    f.first()
                        .copied()
                        .filter(|x| !x.is_empty())
                        .unwrap_or_else(|| {
                            Path::new(wav)
                                .file_stem()
                                .and_then(|s| s.to_str())
                                .unwrap_or("")
                        }),
                );
                if alias.is_empty() {
                    continue;
                }
                let Ok(wav_path) = fs::canonicalize(oto_path.parent().unwrap().join(wav.trim()))
                else {
                    continue;
                };
                if !wav_path.starts_with(&base) || !wav_path.is_file() {
                    continue;
                }
                let e = OtoEntry {
                    alias: alias.clone(),
                    wav_path,
                    offset_ms: numeric(f.get(1)).max(0.0),
                    consonant_ms: numeric(f.get(2)).max(0.0),
                    cutoff_ms: numeric(f.get(3)),
                    preutterance_ms: numeric(f.get(4)).max(0.0),
                    overlap_ms: numeric(f.get(5)).max(0.0),
                };
                entries.entry(alias.clone()).or_insert_with(|| e.clone());
                if !suffix.is_empty() {
                    if let Some(canonical) = alias.strip_suffix(&suffix) {
                        let canonical = normalized(canonical);
                        if !canonical.is_empty() {
                            entries.entry(canonical).or_insert_with(|| e.clone());
                        }
                    }
                }
                let key = folded(&alias);
                match fold.get(&key) {
                    None => {
                        fold.insert(key, Some(e.clone()));
                    }
                    Some(Some(previous)) if previous.alias != alias => {
                        fold.insert(key, None);
                    }
                    _ => (),
                };
                digest.update(e.cache_identity().as_bytes());
            }
        }
        let count = entries
            .values()
            .map(|e| (&e.alias, &e.wav_path))
            .collect::<HashSet<_>>()
            .len();
        if count < options.minimum_aliases.max(1) {
            return Err(format!(
                "voicebank possui poucos aliases válidos ({count} < {})",
                options.minimum_aliases
            ));
        }
        let mut name = "Kasane Teto".to_owned();
        let preferred = base.join("character.txt");
        let character = if preferred.is_file() {
            Some(preferred)
        } else {
            characters.into_iter().next()
        };
        if let Some(path) = character {
            if let Ok(text) = decode(&path) {
                for l in text.lines() {
                    if l.to_lowercase().starts_with("name=") {
                        let v = l.split_once('=').unwrap().1.trim();
                        if !v.is_empty() {
                            name = v.chars().take(120).collect();
                        }
                        break;
                    }
                }
            }
        }
        Ok(Self {
            root: base,
            entries,
            folded_entries: if options.case_sensitive {
                HashMap::new()
            } else {
                fold.into_iter()
                    .filter_map(|(k, v)| v.map(|v| (k, v)))
                    .collect()
            },
            fingerprint: format!("{:x}", digest.finalize()),
            name,
            pitch_map,
        })
    }
    pub fn alias_count(&self) -> usize {
        self.entries
            .values()
            .map(|e| (&e.alias, &e.wav_path))
            .collect::<HashSet<_>>()
            .len()
    }
    fn lookup(&self, key: &str) -> Option<&OtoEntry> {
        self.entries
            .get(key)
            .or_else(|| self.folded_entries.get(&folded(key)))
    }
    pub fn resolve(
        &self,
        candidates: &[String],
        pitch: Option<&str>,
    ) -> Result<Option<OtoEntry>, String> {
        let number = pitch.and_then(pitch_number);
        if pitch.is_some() && !self.pitch_map.is_empty() && number.is_none() {
            return Err("nota inválida para escolher o registro da voicebank".into());
        }
        let mut registers: BTreeMap<(String, String), Vec<i32>> = BTreeMap::new();
        for (n, pair) in &self.pitch_map {
            registers.entry(pair.clone()).or_default().push(*n);
        }
        let mut ordered: Vec<_> = registers.keys().cloned().collect();
        if let Some(n) = number {
            let target = self.pitch_map.get(&n);
            ordered.sort_by_key(|p| {
                (
                    target.map(|t| p != t).unwrap_or(false),
                    registers[p]
                        .iter()
                        .map(|r| (n - r).abs())
                        .min()
                        .unwrap_or(0),
                    p.clone(),
                )
            });
        } else {
            ordered.sort_by_key(|p| (!p.0.is_empty() || !p.1.is_empty(), p.clone()));
        }
        for candidate in candidates {
            let key = normalized(candidate);
            if key.is_empty() {
                continue;
            }
            if registers.is_empty() {
                if let Some(e) = self.lookup(&key) {
                    return Ok(Some(e.clone()));
                }
                continue;
            }
            let mut canonical = key.clone();
            let mut affixes: Vec<_> = registers.keys().collect();
            affixes.sort_by_key(|p| std::cmp::Reverse(p.0.len() + p.1.len()));
            for (pre, suf) in affixes {
                if pre.is_empty() && suf.is_empty() {
                    continue;
                }
                if let Some(s) = key
                    .strip_prefix(pre.as_str())
                    .and_then(|s| s.strip_suffix(suf.as_str()))
                {
                    if !s.is_empty()
                        && registers
                            .keys()
                            .any(|(a, b)| self.lookup(&format!("{a}{s}{b}")).is_some())
                    {
                        canonical = s.to_owned();
                        break;
                    }
                }
            }
            for (pre, suf) in &ordered {
                if let Some(e) = self.lookup(&format!("{pre}{canonical}{suf}")) {
                    let mut e = e.clone();
                    if number.is_none() {
                        e.alias = canonical;
                    }
                    return Ok(Some(e));
                }
            }
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pitch_limits() {
        assert_eq!(pitch_number("C4"), Some(60));
        assert_eq!(pitch_number("Bb3"), Some(58));
        assert_eq!(pitch_number("C20"), None);
    }
    #[test]
    fn oto_case_collisions_do_not_change_xsampa() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("a.wav"), b"wav").unwrap();
        fs::write(
            temp.path().join("oto.ini"),
            "a.wav=E,0,1,-3,4,2\na.wav=e,0,1,-3,4,2\n",
        )
        .unwrap();
        let idx = VoicebankIndex::load(
            temp.path(),
            &VoicebankOptions {
                minimum_aliases: 1,
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(
            idx.resolve(&["E".into()], None).unwrap().unwrap().alias,
            "E"
        );
        assert_eq!(
            idx.resolve(&["e".into()], None).unwrap().unwrap().alias,
            "e"
        );
    }
    #[test]
    fn pitch_map_selects_register_but_lexical_probe_stays_canonical() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("a.wav"), b"wav").unwrap();
        fs::write(
            temp.path().join("oto.ini"),
            "a.wav=a_B3,0,1,-3,4,2\na.wav=a_A4,0,1,-3,4,2\n",
        )
        .unwrap();
        fs::write(temp.path().join("prefix.map"), "B3\t\t_B3\nA4\t\t_A4\n").unwrap();
        let idx = VoicebankIndex::load(
            temp.path(),
            &VoicebankOptions {
                minimum_aliases: 1,
                pitch_map: true,
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(
            idx.resolve(&["a".into()], Some("A4"))
                .unwrap()
                .unwrap()
                .alias,
            "a_A4"
        );
        assert_eq!(
            idx.resolve(&["a".into()], None).unwrap().unwrap().alias,
            "a"
        );
    }
}
