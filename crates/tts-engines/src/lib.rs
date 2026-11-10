//! Native TTS engine boundaries. No engine execution path invokes Python.
pub mod esper;
pub mod process;
pub mod worldline;

use std::fmt;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

#[derive(Debug)]
pub enum EngineError {
    Invalid(String),
    Io(std::io::Error),
    Protocol(String),
    Native(String),
    Timeout,
    Cancelled,
    Exited,
}
impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(s) => write!(f, "invalid engine request: {s}"),
            Self::Io(e) => write!(f, "engine I/O: {e}"),
            Self::Protocol(s) => write!(f, "engine protocol: {s}"),
            Self::Native(s) => write!(f, "native engine: {s}"),
            Self::Timeout => f.write_str("engine deadline exceeded"),
            Self::Cancelled => f.write_str("engine cancelled"),
            Self::Exited => f.write_str("engine process exited or closed its response"),
        }
    }
}
impl std::error::Error for EngineError {}
impl From<std::io::Error> for EngineError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}
pub type Result<T> = std::result::Result<T, EngineError>;

#[derive(Clone, Default, Debug)]
pub struct Cancellation(Arc<AtomicBool>);
impl Cancellation {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}
