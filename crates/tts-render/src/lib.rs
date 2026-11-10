//! Native, platform-independent TTV planning and PCM rendering.
//!
//! External UTAU engines remain native executables. No part of this crate
//! imports Python or starts a Python process.
pub mod dsp;
pub mod cd_pcm;
pub mod environment;
pub mod phonetics;
pub mod prosody;
pub mod renderer;
pub mod timeline;
pub mod voicebank;
pub mod worldline;
pub mod midi;

pub const SAMPLE_RATE: u32 = 44_100;

/// Python's round(): preserve the published PCM's half-to-even convention.
pub fn python_round(value: f64) -> f64 {
    value.round_ties_even()
}
