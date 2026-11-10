//! Scalar PCM equivalents of the renderer and osaka_cd NumPy paths.
use crate::{python_round, SAMPLE_RATE};
use serde::{Deserialize, Serialize};
use std::f64::consts::FRAC_PI_2;

fn clipped(value: f64) -> i16 {
    value.clamp(-32768.0, 32767.0) as i16
}
pub fn decode_pcm16(pcm: &[u8]) -> Result<Vec<i16>, String> {
    if pcm.len() % 2 != 0 {
        return Err("invalid_mono_pcm16".into());
    }
    Ok(pcm
        .chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect())
}
pub fn encode_pcm16(samples: &[i16]) -> Vec<u8> {
    samples.iter().flat_map(|x| x.to_le_bytes()).collect()
}
pub fn apply_gain(samples: &[i16], gain: f64) -> Result<Vec<i16>, String> {
    if !gain.is_finite() {
        return Err("nonfinite_pcm_gain".into());
    }
    if (gain - 1.0).abs() < 0.002 {
        return Ok(samples.to_vec());
    }
    let gain = gain.clamp(0.60, 1.20);
    Ok(samples
        .iter()
        .map(|&x| clipped((x as f64 * gain).trunc()))
        .collect())
}
pub fn scale_pcm16(samples: &[i16], normalization_gain: f64) -> Result<Vec<i16>, String> {
    if !normalization_gain.is_finite() || normalization_gain <= 0.0 {
        return Err("invalid_normalization_gain".into());
    }
    Ok(samples
        .iter()
        .map(|&x| clipped(python_round(x as f64 / normalization_gain)))
        .collect())
}
pub fn rms(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    (samples.iter().map(|&x| (x as f64).powi(2)).sum::<f64>() / samples.len() as f64).sqrt()
}
pub fn match_fragment_energy(
    target: &[i16],
    fragment: &[i16],
    start: isize,
    fade_samples: usize,
    role: &str,
) -> (Vec<i16>, f64, bool) {
    if start < 0 || start as usize >= target.len() || fragment.is_empty() {
        return (fragment.to_vec(), 0.0, false);
    }
    let start = start as usize;
    let overlap = fragment.len().min(target.len() - start);
    if overlap <= 16 {
        return (fragment.to_vec(), 0.0, false);
    }
    let window = overlap.min(
        fade_samples
            .max(1)
            .min(python_round(SAMPLE_RATE as f64 * 0.010) as usize)
            .max(96),
    );
    let a = rms(&target[start..start + window]);
    let b = rms(&fragment[..window]);
    if a < 48.0 || b < 48.0 {
        return (fragment.to_vec(), 0.0, false);
    }
    let delta = 20.0 * (b / a).max(1e-6).log10();
    if delta.abs() < 1.25 {
        return (fragment.to_vec(), delta.abs(), false);
    }
    let gain = if role == "nucleus" {
        (a / b).clamp(0.82, 1.18)
    } else {
        (a / b).clamp(0.86, 1.14)
    };
    if (gain - 1.0).abs() < 0.015 {
        return (fragment.to_vec(), delta.abs(), false);
    }
    (
        apply_gain(fragment, gain).expect("finite local RMS"),
        delta.abs(),
        true,
    )
}
pub fn append_crossfade(target: &mut Vec<i16>, fragment: &[i16], overlap_samples: usize) {
    let overlap = overlap_samples.min(target.len()).min(fragment.len());
    let start = target.len() - overlap;
    for i in 0..overlap {
        let ratio = (i + 1) as f64 / (overlap + 1) as f64;
        let ratio = ratio * ratio * (3.0 - 2.0 * ratio);
        target[start + i] = clipped(
            (target[start + i] as f64 * (1.0 - ratio) + fragment[i] as f64 * ratio).trunc(),
        );
    }
    target.extend_from_slice(&fragment[overlap..]);
}
pub fn place_timeline_fragment(
    target: &mut Vec<i16>,
    fragment: &[i16],
    start_sample: isize,
    fade_samples: usize,
) {
    let source_offset = if start_sample < 0 {
        fragment.len().min(start_sample.unsigned_abs())
    } else {
        0
    };
    let fragment = &fragment[source_offset..];
    if fragment.is_empty() {
        return;
    }
    let start = start_sample.max(0) as usize;
    if start > target.len() {
        target.resize(start, 0);
    }
    let overlap = fragment.len().min(target.len().saturating_sub(start));
    let fade = overlap.min(fade_samples);
    for i in 0..fade {
        let ratio = (((i + 1) as f64 / (fade + 1) as f64) * FRAC_PI_2)
            .sin()
            .powi(2);
        target[start + i] = clipped(
            (target[start + i] as f64 * (1.0 - ratio) + fragment[i] as f64 * ratio).trunc(),
        );
    }
    if start + fragment.len() > target.len() {
        target.resize(start + fragment.len(), 0);
    }
    target[start + fade..start + fragment.len()].copy_from_slice(&fragment[fade..]);
}
pub fn fade_timeline_gaps(samples: &mut [i16], intervals: &[(isize, isize)]) -> usize {
    let mut intervals = intervals.to_vec();
    intervals.sort();
    let mut regions: Vec<(usize, usize)> = Vec::new();
    for (a, b) in intervals {
        let a = a.max(0).min(samples.len() as isize) as usize;
        let b = b.max(a as isize).min(samples.len() as isize) as usize;
        if b <= a {
            continue;
        }
        if let Some(last) = regions.last_mut() {
            if a <= last.1 {
                last.1 = last.1.max(b);
                continue;
            }
        }
        regions.push((a, b));
    }
    let edge = python_round(SAMPLE_RATE as f64 * 0.004) as usize;
    let pause = python_round(SAMPLE_RATE as f64 * 0.024) as usize;
    let mut count = 0;
    for pair in regions.windows(2) {
        let (a, b) = pair[0];
        let (c, d) = pair[1];
        if c - b < pause {
            continue;
        }
        let outgoing = edge.min((b - a) / 2);
        let incoming = edge.min((d - c) / 2);
        for (length, boundary, step) in [(outgoing, b - 1, -1isize), (incoming, c, 1)] {
            for i in 0..length {
                let ratio = (i as f64 / (length.saturating_sub(1).max(1)) as f64 * FRAC_PI_2)
                    .sin()
                    .powi(2);
                let p = (boundary as isize + step * i as isize) as usize;
                samples[p] = clipped(python_round(samples[p] as f64 * ratio));
            }
        }
        count += usize::from(outgoing > 0 || incoming > 0);
    }
    count
}
pub fn phrase_micro_fades(samples: &mut [i16]) {
    let edge = (samples.len() / 2).min(python_round(SAMPLE_RATE as f64 * 0.004) as usize);
    for i in 0..edge {
        let ratio = (i + 1) as f64 / edge.max(1) as f64;
        let end = samples.len() - 1 - i;
        samples[i] = clipped((samples[i] as f64 * ratio).trunc());
        samples[end] = clipped((samples[end] as f64 * ratio).trunc());
    }
}
pub fn performance_envelope(
    samples: &mut [i16],
    start: isize,
    end: isize,
    fade_in: bool,
    fade_out: bool,
    sample_offset: usize,
    total_frames: usize,
) {
    let start = start.max(0) as usize;
    let end = (end.max(0) as usize).min(total_frames);
    if end <= start {
        return;
    }
    let length = ((end - start) / 3).min(python_round(SAMPLE_RATE as f64 * 0.26) as usize);
    for i in 0..length {
        let gain = 0.12
            + 0.88
                * (i as f64 / length.saturating_sub(1).max(1) as f64 * FRAC_PI_2)
                    .sin()
                    .powi(2);
        for (enabled, pos) in [(fade_in, start + i), (fade_out, end - 1 - i)] {
            if enabled && pos >= sample_offset && pos - sample_offset < samples.len() {
                let p = pos - sample_offset;
                samples[p] = clipped(python_round(samples[p] as f64 * gain));
            }
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VelocityEvent {
    pub start_ms: f64,
    pub duration_ms: f64,
    pub velocity: u8,
}
pub fn apply_midi_velocity(
    samples: &mut [i16],
    notes: &[VelocityEvent],
    preroll: f64,
    sample_offset: usize,
    total_frames: usize,
) -> Result<(), String> {
    let mut previous: Option<(f64, usize)> = None;
    for event in notes {
        if !event.start_ms.is_finite()
            || !event.duration_ms.is_finite()
            || event.duration_ms < 0.0
            || event.velocity == 0
            || event.velocity > 127
        {
            return Err("invalid_velocity_event".into());
        }
        let first = python_round(SAMPLE_RATE as f64 * (preroll + event.start_ms) / 1000.0).max(0.0)
            as usize;
        let end = (python_round(
            SAMPLE_RATE as f64 * (preroll + event.start_ms + event.duration_ms) / 1000.0,
        )
        .max(0.0) as usize)
            .min(total_frames);
        let gain = 0.72 + 0.28 * (event.velocity as f64 / 127.0).sqrt();
        let incoming = previous
            .filter(|(_, last)| {
                first as isize - *last as isize <= python_round(SAMPLE_RATE as f64 * 0.02) as isize
            })
            .map(|(g, _)| g)
            .unwrap_or(gain);
        let smooth = end
            .saturating_sub(first)
            .min(python_round(SAMPLE_RATE as f64 * 0.008) as usize);
        let mut ramp_end = end.min(first + smooth.saturating_sub(1).max(1));
        if incoming != gain {
            for p in first.max(sample_offset)..ramp_end.min(sample_offset + samples.len()) {
                let ratio = ((p - first) as f64 / smooth.saturating_sub(1).max(1) as f64
                    * FRAC_PI_2)
                    .sin()
                    .powi(2);
                samples[p - sample_offset] = clipped(python_round(
                    samples[p - sample_offset] as f64 * (incoming + (gain - incoming) * ratio),
                ));
            }
        } else {
            ramp_end = first;
        }
        let tail_gain = incoming + (gain - incoming);
        if tail_gain != 1.0 {
            for p in ramp_end.max(sample_offset)..end.min(sample_offset + samples.len()) {
                samples[p - sample_offset] =
                    clipped(python_round(samples[p - sample_offset] as f64 * tail_gain));
            }
        }
        previous = Some((gain, end));
    }
    Ok(())
}
/// Mix with explicit clipping. Existing authored plans keep peaks below i16;
/// overflow in arbitrary requests is clipped rather than wrapping channels.
pub fn stereo_mix(
    items: &[(&[i16], f64, f64)],
    start: usize,
    stop: usize,
) -> Result<Vec<i16>, String> {
    if stop < start
        || items
            .iter()
            .any(|(s, l, r)| s.len() < stop || !l.is_finite() || !r.is_finite())
    {
        return Err("invalid_mix_window".into());
    }
    let mut output = Vec::with_capacity((stop - start) * 2);
    for i in start..stop {
        let (mut l, mut r) = (0.0, 0.0);
        for (s, lg, rg) in items {
            l += s[i] as f64 * lg;
            r += s[i] as f64 * rg;
        }
        output.push(clipped(python_round(l)));
        output.push(clipped(python_round(r)));
    }
    Ok(output)
}
pub fn normalize_peak(samples: &mut [i16]) -> f64 {
    let peak = samples.iter().map(|x| (*x as i32).abs()).max().unwrap_or(0);
    if peak == 0 {
        return 1.0;
    }
    let gain = (30000.0 / peak as f64).min(1.8);
    if (gain - 1.0).abs() > 0.01 {
        for x in samples {
            *x = clipped((*x as f64 * gain).trunc());
        }
    }
    gain
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rounding_is_python_compatible() {
        assert_eq!(
            scale_pcm16(&[1, 3, -1, -3], 2.0).unwrap(),
            vec![0, 2, 0, -2]
        );
        assert_eq!(apply_gain(&[3, -3], 0.6).unwrap(), vec![1, -1]);
    }
    #[test]
    fn placement_replaces_after_fade_and_respects_negative_time() {
        let mut a = vec![100; 5];
        place_timeline_fragment(&mut a, &[200; 4], -2, 2);
        assert_eq!(a.len(), 5);
        assert_eq!(&a[2..], &[100, 100, 100]);
        assert!(a[0] > 100);
    }
    #[test]
    fn crossfade_keeps_reference_truncation() {
        let mut a = vec![123; 10];
        append_crossfade(&mut a, &[123; 10], 5);
        assert_eq!(
            a,
            vec![123, 123, 123, 123, 123, 123, 122, 123, 123, 123, 123, 123, 123, 123, 123]
        );
    }
    #[test]
    fn mix_keeps_stereo_independent() {
        let a = [1, 2, 3];
        let b = [3, 2, 1];
        assert_eq!(
            stereo_mix(&[(&a, 1.0, 0.0), (&b, 0.0, 1.0)], 0, 3).unwrap(),
            vec![1, 3, 2, 2, 3, 1]
        );
    }
    #[test]
    fn windows_envelope_equal_whole() {
        let mut whole = vec![10000; 1000];
        performance_envelope(&mut whole, 0, 1000, true, true, 0, 1000);
        let mut left = vec![10000; 400];
        let mut right = vec![10000; 600];
        performance_envelope(&mut left, 0, 1000, true, true, 0, 1000);
        performance_envelope(&mut right, 0, 1000, true, true, 400, 1000);
        left.extend(right);
        assert_eq!(left, whole);
    }
    #[test]
    fn production_pcm_contracts() {
        let cases: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/pcm-contract.json")).unwrap();
        for (cn, case) in cases.as_array().unwrap().iter().enumerate() {
            let samples: Vec<i16> = serde_json::from_value(case["samples"].clone()).unwrap();
            let mut result = samples.clone();
            match case["op"].as_str().unwrap() {
                "gain" => result = apply_gain(&samples, case["gain"].as_f64().unwrap()).unwrap(),
                "crossfade" => {
                    let f: Vec<i16> = serde_json::from_value(case["fragment"].clone()).unwrap();
                    append_crossfade(&mut result, &f, case["overlap"].as_u64().unwrap() as usize);
                }
                "place" => {
                    let f: Vec<i16> = serde_json::from_value(case["fragment"].clone()).unwrap();
                    place_timeline_fragment(
                        &mut result,
                        &f,
                        case["start"].as_i64().unwrap() as isize,
                        case["fade"].as_u64().unwrap() as usize,
                    );
                }
                "envelope" => {
                    let n = result.len();
                    performance_envelope(&mut result, 0, n as isize, true, true, 0, n);
                }
                "velocity" => {
                    let notes: Vec<VelocityEvent> =
                        serde_json::from_value(case["events"].clone()).unwrap();
                    let n = result.len();
                    apply_midi_velocity(&mut result, &notes, 0.0, 0, n).unwrap();
                }
                other => panic!("unknown fixture {other}"),
            }
            let expected: Vec<i16> = serde_json::from_value(case["expected"].clone()).unwrap();
            assert_eq!(result, expected, "PCM contract {cn}: {}", case["op"]);
        }
    }
}
