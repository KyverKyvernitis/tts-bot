//! Optional short leading phrase and one bounded, ordered lookahead.
use crate::{AudioSink, ProviderError, ProviderMetadata, Record, Result};
use std::{
    future::Future,
    sync::{Arc, RwLock},
};
use tokio::{
    sync::{mpsc, OwnedSemaphorePermit, Semaphore},
    task::JoinHandle,
};
use unicode_normalization::char::is_combining_mark;

const MIN_TEXT_CHARS: usize = 120;
const MIN_LEAD_CHARS: usize = 40;
const MAX_LEAD_CHARS: usize = 70;
const SEGMENT_QUEUE_CHUNKS: usize = 4;

/// Split only at a safe word boundary before either provider's own escaping and
/// tokenization. Trimming the boundary leaves all spoken characters intact.
pub(crate) fn startup_split(text: &str) -> Option<(&str, &str)> {
    let text = text.trim();
    if text.chars().take(MIN_TEXT_CHARS).count() < MIN_TEXT_CHARS {
        return None;
    }
    let mut word_boundary = None;
    let mut previous_was_space = false;
    for (count, (offset, character)) in text.char_indices().enumerate().take(MAX_LEAD_CHARS + 1) {
        let is_space = character.is_whitespace();
        if is_space && !previous_was_space && count >= MIN_LEAD_CHARS {
            let left = &text[..offset];
            let right = text[offset..].trim_start();
            if let Some(punctuation) = safe_boundary(left, right) {
                if punctuation {
                    return Some((left, right));
                }
                word_boundary = Some((left, right));
            }
        }
        previous_was_space = is_space;
    }
    word_boundary
}

fn safe_boundary(left: &str, right: &str) -> Option<bool> {
    let left_word = left.split_whitespace().next_back()?;
    let right_word = right.split_whitespace().next()?;
    let last = left_word.chars().next_back()?;
    let next = right.chars().next()?;
    // Keep combining marks, number/unit pairs, hyphenated line breaks and
    // punctuation clusters intact.
    if !(next.is_alphabetic()
        || matches!(next, '"' | '\'' | '(' | '[' | '«' | '“' | '‘' | '¡' | '¿'))
        || is_combining_mark(next)
        || left_word.chars().any(char::is_numeric)
        || right_word.chars().any(char::is_numeric)
        || matches!(last, '-' | '‐' | '‑' | '–' | '/' | '\\')
        || !left_word.chars().any(char::is_alphabetic)
    {
        return None;
    }
    // Google strips some abbreviation dots before calling the splitter.
    // Recognize both forms to keep titles attached to the following word.
    let stem = left_word.strip_suffix('.').unwrap_or(left_word);
    let short = stem.to_lowercase();
    if matches!(
        short.as_str(),
        "dr" | "dra"
            | "sr"
            | "sra"
            | "srta"
            | "prof"
            | "profa"
            | "jr"
            | "mr"
            | "mrs"
            | "ms"
            | "msgr"
            | "st"
            | "esq"
            | "esquire"
            | "ex"
            | "art"
            | "etc"
            | "pág"
            | "fig"
    ) {
        return None;
    }
    if last == '.' {
        let dotted = stem.contains('.')
            && stem
                .split('.')
                .all(|part| part.chars().count() == 1 && part.chars().all(char::is_alphabetic));
        if dotted || (stem.chars().count() == 1 && stem.chars().all(char::is_alphabetic)) {
            return None;
        }
    }
    Some(matches!(
        last,
        '.' | '!' | '?' | ',' | ':' | ';' | '…' | '。' | '！' | '？' | '，'
    ))
}

struct Segment {
    receiver: mpsc::Receiver<Record>,
    task: JoinHandle<()>,
}

impl Drop for Segment {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn start_segment<Fut>(
    part: String,
    parent: &AudioSink,
    primary: bool,
    permit: Option<OwnedSemaphorePermit>,
    synthesize: &impl Fn(String, AudioSink) -> Fut,
) -> Segment
where
    Fut: Future<Output = Result<()>> + Send + 'static,
{
    let (sender, receiver) = mpsc::channel(SEGMENT_QUEUE_CHUNKS);
    let metadata = if primary {
        parent.metadata.clone()
    } else {
        Arc::new(RwLock::new(ProviderMetadata::default()))
    };
    let child = AudioSink {
        sender: sender.clone(),
        cancellation: parent.cancellation.clone(),
        limit: parent.limit.saturating_sub(parent.size),
        size: 0,
        metadata,
    };
    let synthesize = synthesize(part, child);
    let task = tokio::spawn(async move {
        let terminal = match synthesize.await {
            Ok(()) => Record::Complete,
            Err(error) => Record::Failed(error),
        };
        // No physical provider work remains. Backpressure on the terminal
        // record should not hold a speculative transport slot open.
        drop(permit);
        let _ = sender.send(terminal).await;
    });
    Segment { receiver, task }
}

fn spare_permit(permits: &Arc<Semaphore>) -> Option<OwnedSemaphorePermit> {
    // Check the reserve atomically with admission. Taking two briefly and
    // returning one leaves a slot for the next foreground request.
    let mut free = permits.clone().try_acquire_many_owned(2).ok()?;
    free.split(1)
}

/// Continue the immediately following provider segment while the first plays.
/// The provider pool holds the original foreground permit for this utterance.
/// The next segment is bounded to four chunks and uses a separate spare slot.
pub(crate) async fn stream_ordered<Fut>(
    parts: Vec<String>,
    parent: &mut AudioSink,
    permits: &Arc<Semaphore>,
    synthesize: impl Fn(String, AudioSink) -> Fut,
) -> Result<()>
where
    Fut: Future<Output = Result<()>> + Send + 'static,
{
    let mut parts = parts.into_iter();
    let Some(first) = parts.next() else {
        return Err(ProviderError::new("empty", "segmentos TTS vazios"));
    };
    let mut current = start_segment(first, parent, true, None, &synthesize);
    let mut second = parts.next();
    let mut ahead = None;
    let mut first_frame = Some(FirstMp3Frame::default());
    let mut segment_has_audio = false;

    loop {
        let received = tokio::select! {
            biased;
            _ = parent.cancellation.cancelled() => return Err(ProviderError::new("cancelled", "TTS cancelado")),
            record = current.receiver.recv() => record,
        };
        match received {
            Some(Record::Audio(audio)) => {
                let ready = first_frame
                    .as_mut()
                    .is_some_and(|start| start.observe(&audio));
                parent.emit(audio).await?;
                segment_has_audio = true;
                if ready {
                    first_frame = None;
                    if second.is_some() {
                        if let Some(permit) = spare_permit(permits) {
                            ahead = Some(start_segment(
                                second.take().unwrap(),
                                parent,
                                false,
                                Some(permit),
                                &synthesize,
                            ));
                        }
                    }
                }
            }
            Some(Record::Complete) if segment_has_audio => {
                first_frame = None;
                segment_has_audio = false;
                if let Some(segment) = ahead.take() {
                    current = segment;
                } else if let Some(part) = second.take().or_else(|| parts.next()) {
                    current = start_segment(part, parent, false, None, &synthesize);
                } else {
                    return Ok(());
                }
            }
            Some(Record::Complete) => {
                return Err(ProviderError::new("empty", "segmento TTS sem áudio"))
            }
            Some(Record::Failed(error)) => return Err(error),
            None => {
                return Err(ProviderError::new(
                    "incomplete",
                    "segmento TTS encerrou sem confirmar conclusão",
                ))
            }
        }
    }
}

/// Only triggers lookahead with a complete Layer III frame. MP3 tags, network
/// chunk boundaries and unrelated provider bytes do not start a second call.
#[derive(Default)]
struct FirstMp3Frame {
    prefix: Vec<u8>,
    rejected: bool,
}

impl FirstMp3Frame {
    fn observe(&mut self, bytes: &[u8]) -> bool {
        const LIMIT: usize = 16 * 1024;
        if self.rejected {
            return false;
        }
        let room = LIMIT - self.prefix.len();
        self.prefix
            .extend_from_slice(&bytes[..bytes.len().min(room)]);
        let prefix = self.prefix.as_slice();
        if prefix.len() < 4 {
            return false;
        }
        let offset = if prefix.starts_with(b"ID3") {
            if prefix.len() < 10 {
                return false;
            }
            if prefix[6..10].iter().any(|byte| byte & 0x80 != 0) {
                self.rejected = true;
                return false;
            }
            let size = prefix[6..10]
                .iter()
                .fold(0usize, |n, value| (n << 7) | usize::from(*value));
            10 + size
                + if prefix[3] == 4 && prefix[5] & 0x10 != 0 {
                    10
                } else {
                    0
                }
        } else {
            0
        };
        if offset + 4 > LIMIT {
            self.rejected = true;
            return false;
        }
        if prefix.len() < offset + 4 {
            return false;
        }
        let Some(size) = layer_three_frame_size(&prefix[offset..offset + 4]) else {
            self.rejected = true;
            return false;
        };
        if offset + size > LIMIT {
            self.rejected = true;
            return false;
        }
        prefix.len() >= offset + size
    }
}

fn layer_three_frame_size(header: &[u8]) -> Option<usize> {
    if header[0] != 0xff || header[1] & 0xe6 != 0xe2 {
        return None;
    }
    let version = (header[1] >> 3) & 3;
    let rate_index = (header[2] >> 2) & 3;
    let bitrate_index = usize::from(header[2] >> 4);
    if version == 1 || rate_index == 3 || bitrate_index == 0 || bitrate_index == 15 {
        return None;
    }
    let mpeg1 = version == 3;
    let rates = [44_100, 48_000, 32_000];
    let rate = rates[usize::from(rate_index)]
        / if mpeg1 {
            1
        } else if version == 2 {
            2
        } else {
            4
        };
    let bitrates = if mpeg1 {
        [
            0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320,
        ]
    } else {
        [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    };
    let numerator = if mpeg1 { 144_000 } else { 72_000 };
    Some(numerator * bitrates[bitrate_index] / rate + usize::from((header[2] >> 1) & 1))
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;
    use std::time::Duration;
    use tokio::sync::Notify;
    use tokio_util::sync::CancellationToken;

    #[test]
    fn cuts_at_punctuation_and_retains_the_rest_exactly() {
        let lead = "Esta mensagem começa com uma breve saudação.";
        let tail = " Depois ela continua por várias outras palavras e mantém seu conteúdo, mesmo com a próxima parte preparada em paralelo.";
        let text = format!("{lead}{tail}");
        assert_eq!(startup_split(&text), Some((lead, tail.trim_start())));
        assert!(startup_split(lead).is_none());
    }

    #[test]
    fn skips_abbreviations_numbers_and_combining_and_uses_word_boundaries() {
        let stem = "Este aviso traz uma explicação com referência";
        let tail = " ao Dr. Silva em 24,5 quilogramas. Depois traga a informação completa sobre esse relatório.";
        let text = format!("{stem}{tail}");
        let (left, right) = startup_split(&text).unwrap();
        assert!(!left.ends_with("Dr."));
        assert!(!left.ends_with("24,5"));
        assert!((40..=70).contains(&left.chars().count()));
        assert_eq!(format!("{left} {right}"), text);
        assert!(safe_boundary("explicação Dr.", "Silva explica").is_none());
        assert!(safe_boundary("explicação Dr", "Silva explica").is_none());
        assert!(safe_boundary("sigla S.P.", "fica aqui").is_none());
        assert!(safe_boundary("texto", "\u{301}casa").is_none());
        assert!(safe_boundary("texto", "\u{345}casa").is_none());
        assert!(safe_boundary("abc-", "palavra seguinte").is_none());
        assert!(startup_split(&"x".repeat(180)).is_none());
    }

    fn mp3_frame(id: u8) -> Vec<u8> {
        // MPEG-2 Layer III (48 kb/s, 24 kHz), an internally consistent frame
        // boundary with a marker after its header to check forwarding order.
        let mut frame = vec![id; 144];
        frame[..4].copy_from_slice(&[0xff, 0xf3, 0x64, 0xc0]);
        frame
    }

    #[test]
    fn only_complete_frame_after_id3_triggers_lookahead() {
        let mut start = FirstMp3Frame::default();
        assert!(!start.observe(b"ID3\x04\0\0\0\0\0"));
        assert!(!start.observe(b"\x02ab\xff"));
        let frame = mp3_frame(0);
        assert!(!start.observe(&frame[1..143]));
        assert!(start.observe(&frame[143..]));
        let mut invalid = FirstMp3Frame::default();
        assert!(!invalid.observe(b"HTTP response is not MP3"));
        assert!(!invalid.observe(&frame));
    }

    fn sink() -> (AudioSink, mpsc::Receiver<Record>) {
        let (sender, receiver) = mpsc::channel(8);
        (
            AudioSink {
                sender,
                cancellation: CancellationToken::new(),
                limit: 4096,
                size: 0,
                metadata: Arc::new(RwLock::new(ProviderMetadata::default())),
            },
            receiver,
        )
    }

    #[tokio::test]
    async fn starts_one_spare_segment_after_audio_and_releases_it_in_order() {
        let (mut sink, mut receiver) = sink();
        let semaphore = Arc::new(Semaphore::new(3));
        let _foreground = semaphore.clone().try_acquire_owned().unwrap();
        let started_second = Arc::new(Notify::new());
        let synthesize = |part: String, mut child: AudioSink| {
            let started_second = started_second.clone();
            async move {
                let id = part.parse::<u8>().unwrap();
                child.emit(mp3_frame(id)).await?;
                if id == 1 {
                    started_second.notified().await;
                    child.emit(mp3_frame(id + 1)).await?;
                } else if id == 3 {
                    started_second.notify_one();
                }
                Ok(())
            }
        };
        tokio::time::timeout(
            Duration::from_secs(1),
            stream_ordered(
                vec!["1".into(), "3".into(), "4".into()],
                &mut sink,
                &semaphore,
                synthesize,
            ),
        )
        .await
        .unwrap()
        .unwrap();
        let mut received = Vec::new();
        while let Ok(Record::Audio(data)) = receiver.try_recv() {
            received.push(data[4]);
        }
        assert_eq!(received, [1, 2, 3, 4]);
        assert_eq!(semaphore.available_permits(), 2);
    }

    #[tokio::test]
    async fn works_serially_with_only_the_foreground_permit_and_propagates_late_failure() {
        let (mut sink, mut receiver) = sink();
        let semaphore = Arc::new(Semaphore::new(1));
        let _foreground = semaphore.clone().try_acquire_owned().unwrap();
        let handler = |part: String, mut child: AudioSink| async move {
            child.emit(Bytes::from(mp3_frame(1))).await?;
            if part == "failed" {
                return Err(ProviderError::new("network", "fixture disconnected"));
            }
            Ok(())
        };
        let error = stream_ordered(
            vec!["first".into(), "failed".into()],
            &mut sink,
            &semaphore,
            handler,
        )
        .await
        .unwrap_err();
        assert_eq!(error.kind, "network");
        assert!(matches!(receiver.try_recv(), Ok(Record::Audio(_))));
        assert!(matches!(receiver.try_recv(), Ok(Record::Audio(_))));
        assert!(receiver.try_recv().is_err());
    }

    #[tokio::test]
    async fn holds_the_last_slot_for_foreground_even_for_valid_audio() {
        use std::sync::atomic::{AtomicBool, Ordering};

        let (mut sink, mut receiver) = sink();
        let semaphore = Arc::new(Semaphore::new(2));
        let _foreground = semaphore.clone().try_acquire_owned().unwrap();
        let first_done = Arc::new(AtomicBool::new(false));
        let gate = Arc::new(Notify::new());
        let synthesize = |part: String, mut child: AudioSink| {
            let first_done = first_done.clone();
            let gate = gate.clone();
            async move {
                if part == "first" {
                    child.emit(mp3_frame(1)).await?;
                    gate.notified().await;
                    first_done.store(true, Ordering::Relaxed);
                } else {
                    assert!(first_done.load(Ordering::Relaxed));
                    child.emit(mp3_frame(2)).await?;
                }
                Ok(())
            }
        };
        tokio::time::timeout(Duration::from_secs(1), async {
            let forward = stream_ordered(
                vec!["first".into(), "second".into()],
                &mut sink,
                &semaphore,
                synthesize,
            );
            let drain = async {
                let Some(Record::Audio(first)) = receiver.recv().await else {
                    panic!("missing first audio")
                };
                assert_eq!(first[4], 1);
                assert!(!first_done.load(Ordering::Relaxed));
                assert_eq!(semaphore.available_permits(), 1);
                gate.notify_one();
                let Some(Record::Audio(second)) = receiver.recv().await else {
                    panic!("missing second audio")
                };
                assert_eq!(second[4], 2);
            };
            let (result, ()) = tokio::join!(forward, drain);
            result.unwrap();
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn cancellation_aborts_running_ahead_segment_and_releases_permit() {
        let (mut sink, _receiver) = sink();
        let semaphore = Arc::new(Semaphore::new(3));
        let _foreground = semaphore.clone().try_acquire_owned().unwrap();
        let stop = sink.cancellation.clone();
        let handler = |part: String, mut child: AudioSink| {
            let stop = stop.clone();
            async move {
                if part == "first" {
                    child.emit(mp3_frame(1)).await?;
                } else {
                    stop.cancel();
                }
                std::future::pending().await
            }
        };
        let error = tokio::time::timeout(
            Duration::from_secs(1),
            stream_ordered(
                vec!["first".into(), "second".into()],
                &mut sink,
                &semaphore,
                handler,
            ),
        )
        .await
        .unwrap()
        .unwrap_err();
        assert_eq!(error.kind, "cancelled");
        let _recovered = tokio::time::timeout(Duration::from_secs(1), semaphore.acquire_many(2))
            .await
            .unwrap()
            .unwrap();
    }
}
