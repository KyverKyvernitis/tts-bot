//! Exact 48 kHz CD/TTV composition kernels, separate from UTAU's 44.1 kHz
//! renderer. No resampling, re-normalization or musical planning happens here.
use crate::python_round;

pub fn stereo_mix(items: &[(&[i16], f64, f64)], frames: usize, numpy_wrap: bool)
    -> Result<Vec<i16>, String> {
    if frames > 128 * 1024 * 1024 / 4 { return Err("invalid_mix_window".into()); }
    if items.iter().any(|(samples, l, r)| samples.len() != frames || !l.is_finite() || !r.is_finite()) {
        return Err("invalid_mix_window".into());
    }
    let capacity = frames.checked_mul(2).ok_or("invalid_mix_window")?;
    let mut result = Vec::with_capacity(capacity);
    for index in 0..frames {
        let (mut left, mut right) = (0.0, 0.0);
        for (samples, left_gain, right_gain) in items {
            left += samples[index] as f64 * left_gain;
            right += samples[index] as f64 * right_gain;
        }
        for value in [left, right] {
            let value = python_round(value);
            if !value.is_finite() { return Err("invalid_mix_value".into()); }
            if numpy_wrap {
                // np.rint(float64).astype(int16) wraps finite signed integers;
                // retain that behavior instead of the renderer DSP's clipping.
                result.push((value % 65536.0) as i64 as i16);
            } else {
                if !(-32768.0..=32767.0).contains(&value) {
                    return Err("pcm_int16_overflow".into());
                }
                result.push(value as i16);
            }
        }
    }
    Ok(result)
}

/// The absolute position in an overlap is retained across progressive chunks.
pub fn linear_overlap(old: &[i16], incoming: &[i16], relative: usize, total_overlap: usize)
    -> Result<Vec<i16>, String> {
    if old.len() > incoming.len() || relative > total_overlap
        || old.len() > total_overlap.saturating_sub(relative) {
        return Err("invalid_pcm_overlap".into());
    }
    let denominator = total_overlap.saturating_sub(1).max(1) as f64;
    let mut result = incoming.to_vec();
    for index in 0..old.len() {
        let ratio = (relative + index) as f64 / denominator;
        let value = python_round(old[index] as f64 * (1.0 - ratio) + incoming[index] as f64 * ratio);
        if !(-32768.0..=32767.0).contains(&value) { return Err("pcm_int16_overflow".into()); }
        result[index] = value as i16;
    }
    Ok(result)
}

pub fn first_nonzero_stereo_frame(samples: &[i16]) -> Result<Option<usize>, String> {
    if samples.len() % 2 != 0 { return Err("invalid_stereo_pcm16".into()); }
    Ok(samples.chunks_exact(2).position(|frame| frame[0] != 0 || frame[1] != 0))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mix_preserves_half_even_channel_gain_and_legacy_overflow_modes() {
        let samples = [1, 3, -1, -3];
        assert_eq!(stereo_mix(&[(&samples, 0.5, 1.0)], 4, false).unwrap(),
                   vec![0, 1, 2, 3, 0, -1, -2, -3]);
        assert_eq!(stereo_mix(&[(&[32767, -32768], 2.0, 2.0)], 2, true).unwrap(), vec![-2, -2, 0, 0]);
        assert!(stereo_mix(&[(&[32767], 2.0, 2.0)], 1, false).is_err());
    }
    #[test]
    fn progressive_overlap_equals_one_complete_overlap() {
        let old = [1, 3, -3, 12345, -23456, 32767, 21];
        let incoming = [7, -3, 2, -11111, 23456, -32768, -21];
        let whole = linear_overlap(&old, &incoming, 0, 7).unwrap();
        let mut pieces = linear_overlap(&old[..3], &incoming[..3], 0, 7).unwrap();
        pieces.extend(linear_overlap(&old[3..], &incoming[3..], 3, 7).unwrap());
        assert_eq!(pieces, whole);
        assert_eq!(linear_overlap(&[50], &[100, 200, 300], 0, 1).unwrap(), vec![50, 200, 300]);
    }
    #[test]
    fn stereo_scan_uses_frames_and_checks_complete_samples() {
        assert_eq!(first_nonzero_stereo_frame(&[0, 0, 0, -1, 2, 0]).unwrap(), Some(1));
        assert_eq!(first_nonzero_stereo_frame(&[0, 0]).unwrap(), None);
        assert!(first_nonzero_stereo_frame(&[1]).is_err());
    }
}
