//! Vínculo experimental para Python. A lógica e a sincronização ficam no núcleo Rust.
//! Nenhuma integração é feita automaticamente com o bot Discord.

use std::sync::Arc;
use std::time::Duration;

use nucleo_fluxo_tts::{
    ConfiguracaoDoFluxo, ErroDoFluxo, EstadoDoFluxo, FluxoDeAudio, EstatisticasDoFluxo,
};
use pyo3::create_exception;
use pyo3::exceptions::{PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

create_exception!(
    _nucleo_fluxo_tts,
    ErroTempoEsgotado,
    PyTimeoutError,
    "O prazo para inserir ou ler o bloco terminou."
);
create_exception!(
    _nucleo_fluxo_tts,
    ErroFluxoCancelado,
    PyRuntimeError,
    "O fluxo foi cancelado e os blocos pendentes descartados."
);
create_exception!(
    _nucleo_fluxo_tts,
    ErroFluxoFinalizado,
    PyRuntimeError,
    "O fluxo foi finalizado e não aceita novos blocos."
);

const LIMITE_ESPERA_MS: u64 = 3_600_000; // Limita espera individual a uma hora.

fn prazo(tempo_maximo_ms: u64) -> PyResult<Duration> {
    if tempo_maximo_ms > LIMITE_ESPERA_MS {
        return Err(PyValueError::new_err(
            "tempo_maximo_ms deve estar entre 0 e 3600000",
        ));
    }
    Ok(Duration::from_millis(tempo_maximo_ms))
}

fn traduzir_erro(erro: ErroDoFluxo) -> PyErr {
    match erro {
        ErroDoFluxo::ConfiguracaoInvalida | ErroDoFluxo::BlocoMaiorQueCapacidade { .. } => {
            PyValueError::new_err(erro.to_string())
        }
        ErroDoFluxo::TempoEsgotado => ErroTempoEsgotado::new_err(erro.to_string()),
        ErroDoFluxo::Finalizado => ErroFluxoFinalizado::new_err(erro.to_string()),
        ErroDoFluxo::Cancelado => ErroFluxoCancelado::new_err(erro.to_string()),
    }
}

fn estado_texto(estado: EstadoDoFluxo) -> &'static str {
    match estado {
        EstadoDoFluxo::Aberto => "aberto",
        EstadoDoFluxo::Finalizado => "finalizado",
        EstadoDoFluxo::Cancelado => "cancelado",
    }
}

fn estatisticas_para_python<'py>(
    py: Python<'py>,
    dados: EstatisticasDoFluxo,
) -> PyResult<Bound<'py, PyDict>> {
    let saida = PyDict::new(py);
    saida.set_item("estado", estado_texto(dados.estado))?;
    saida.set_item("bytes_em_fila", dados.bytes_em_fila)?;
    saida.set_item("blocos_em_fila", dados.blocos_em_fila)?;
    saida.set_item("maior_ocupacao_bytes", dados.maior_ocupacao_bytes)?;
    saida.set_item("bytes_escritos", dados.bytes_escritos)?;
    saida.set_item("bytes_lidos", dados.bytes_lidos)?;
    saida.set_item("bytes_descartados", dados.bytes_descartados)?;
    saida.set_item("escritas_que_aguardaram", dados.escritas_que_aguardaram)?;
    saida.set_item("leituras_que_aguardaram", dados.leituras_que_aguardaram)?;
    saida.set_item("prazos_escrita_esgotados", dados.prazos_escrita_esgotados)?;
    saida.set_item("prazos_leitura_esgotados", dados.prazos_leitura_esgotados)?;
    Ok(saida)
}

/// Interface Python do fluxo com um consumidor lógico e múltiplos produtores.
/// Não é um buffer de replay. Nenhuma conexão de rede é aberta.
#[pyclass(module = "tts_fluxo_experimental._nucleo_fluxo_tts", name = "FluxoDeAudio")]
struct FluxoDeAudioPython {
    interno: Arc<FluxoDeAudio>,
    capacidade_bytes: usize,
    maximo_blocos: usize,
}

#[pymethods]
impl FluxoDeAudioPython {
    /// FluxoDeAudio(capacidade_bytes=131072, maximo_blocos=64)
    #[new]
    #[pyo3(signature = (capacidade_bytes=131_072, maximo_blocos=64))]
    fn novo(capacidade_bytes: usize, maximo_blocos: usize) -> PyResult<Self> {
        let configuracao = ConfiguracaoDoFluxo::nova(capacidade_bytes, maximo_blocos)
            .map_err(traduzir_erro)?;
        let interno = FluxoDeAudio::novo(configuracao).map_err(traduzir_erro)?;
        Ok(Self {
            interno: Arc::new(interno),
            capacidade_bytes,
            maximo_blocos,
        })
    }

    /// Enfileira bytes; quando a fila está cheia, aguarda sem reter o GIL.
    #[pyo3(signature = (dados, tempo_maximo_ms=0))]
    fn escrever_bloco(
        &self,
        py: Python<'_>,
        dados: &Bound<'_, PyBytes>,
        tempo_maximo_ms: u64,
    ) -> PyResult<()> {
        let espera = prazo(tempo_maximo_ms)?;
        let dados = dados.as_bytes();
        // Rejeitar antes de alocar uma segunda cópia na fronteira FFI.
        if dados.len() > self.capacidade_bytes {
            return Err(traduzir_erro(ErroDoFluxo::BlocoMaiorQueCapacidade {
                bytes: dados.len(),
                capacidade: self.capacidade_bytes,
            }));
        }
        let bloco = dados.to_vec();
        let fluxo = Arc::clone(&self.interno);
        py.detach(move || fluxo.escrever_bloco(bloco, espera))
            .map_err(traduzir_erro)
    }

    /// Retorna bytes ou None depois do EOF normal; prazo vencido gera exceção.
    #[pyo3(signature = (tempo_maximo_ms=0))]
    fn ler_bloco<'py>(
        &self,
        py: Python<'py>,
        tempo_maximo_ms: u64,
    ) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let espera = prazo(tempo_maximo_ms)?;
        let fluxo = Arc::clone(&self.interno);
        let bloco = py.detach(move || fluxo.ler_bloco(espera))
            .map_err(traduzir_erro)?;
        Ok(bloco.map(|bytes| PyBytes::new(py, &bytes)))
    }

    /// Impede novas escritas, mas permite drenar os blocos antes do EOF.
    fn finalizar(&self, py: Python<'_>) -> PyResult<()> {
        let fluxo = Arc::clone(&self.interno);
        py.detach(move || fluxo.finalizar()).map_err(traduzir_erro)
    }

    /// Descarta blocos em fila e desperta leitores/escritores bloqueados.
    fn cancelar(&self, py: Python<'_>) {
        let fluxo = Arc::clone(&self.interno);
        py.detach(move || fluxo.cancelar());
    }

    /// Snapshot rápido dos contadores, sem esperar rede ou disco.
    fn estatisticas<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dados = self.interno.estatisticas();
        estatisticas_para_python(py, dados)
    }

    #[getter]
    fn capacidade_bytes(&self) -> usize {
        self.capacidade_bytes
    }

    #[getter]
    fn maximo_blocos(&self) -> usize {
        self.maximo_blocos
    }
}

#[pymodule]
fn _nucleo_fluxo_tts(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<FluxoDeAudioPython>()?;
    m.add("ErroTempoEsgotado", m.py().get_type::<ErroTempoEsgotado>())?;
    m.add("ErroFluxoCancelado", m.py().get_type::<ErroFluxoCancelado>())?;
    m.add("ErroFluxoFinalizado", m.py().get_type::<ErroFluxoFinalizado>())?;
    Ok(())
}
