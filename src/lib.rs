//! Nucleo experimental para transportar blocos de audio entre produtor e consumidor.
//!
//! A fila de consumo unico e o replay compartilhado possuem contratos separados.
//! A integracao Python seleciona o backend por operacao, antes da reproducao.

mod configuracao;
mod erro;
mod fluxo;
mod metricas;
pub mod replay;
pub mod source;

pub use configuracao::ConfiguracaoDoFluxo;
pub use erro::ErroDoFluxo;
pub use fluxo::FluxoDeAudio;
pub use metricas::{EstadoDoFluxo, EstatisticasDoFluxo};
