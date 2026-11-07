//! Nucleo experimental para transportar blocos de audio entre produtor e consumidor.
//!
//! Esta biblioteca ainda nao e utilizada pelo bot Python. O fluxo e de consumo
//! unico; o ReplayBuffer compartilhado existente permanece fora deste escopo.

mod configuracao;
mod erro;
mod fluxo;
mod metricas;

pub use configuracao::ConfiguracaoDoFluxo;
pub use erro::ErroDoFluxo;
pub use fluxo::FluxoDeAudio;
pub use metricas::{EstadoDoFluxo, EstatisticasDoFluxo};
