use crate::ErroDoFluxo;

/// Limites de ocupacao do buffer, incluindo o numero de blocos em espera.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConfiguracaoDoFluxo {
    pub capacidade_bytes: usize,
    pub maximo_blocos: usize,
}

impl ConfiguracaoDoFluxo {
    /// Rejeita limites nulos para impedir filas infinitas ou impossiveis.
    pub fn nova(capacidade_bytes: usize, maximo_blocos: usize) -> Result<Self, ErroDoFluxo> {
        if capacidade_bytes == 0 || maximo_blocos == 0 {
            return Err(ErroDoFluxo::ConfiguracaoInvalida);
        }
        Ok(Self { capacidade_bytes, maximo_blocos })
    }
}

impl Default for ConfiguracaoDoFluxo {
    fn default() -> Self {
        Self {
            capacidade_bytes: 128 * 1024,
            maximo_blocos: 64,
        }
    }
}
