use std::error::Error;
use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ErroDoFluxo {
    ConfiguracaoInvalida,
    BlocoMaiorQueCapacidade { bytes: usize, capacidade: usize },
    TempoEsgotado,
    Finalizado,
    Cancelado,
}

impl fmt::Display for ErroDoFluxo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ConfiguracaoInvalida => write!(f, "limites de buffer devem ser positivos"),
            Self::BlocoMaiorQueCapacidade { bytes, capacidade } => {
                write!(f, "bloco de {bytes} bytes excede capacidade de {capacidade} bytes")
            }
            Self::TempoEsgotado => write!(f, "prazo de espera esgotado"),
            Self::Finalizado => write!(f, "fluxo finalizado; escrita indisponivel"),
            Self::Cancelado => write!(f, "fluxo cancelado"),
        }
    }
}

impl Error for ErroDoFluxo {}
