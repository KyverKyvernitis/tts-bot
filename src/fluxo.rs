use std::collections::VecDeque;
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::{ConfiguracaoDoFluxo, ErroDoFluxo, EstadoDoFluxo, EstatisticasDoFluxo};

#[derive(Debug)]
struct Interior {
    estado: EstadoDoFluxo,
    blocos: VecDeque<Vec<u8>>,
    bytes_em_fila: usize,
    maior_ocupacao_bytes: usize,
    bytes_escritos: u64,
    bytes_lidos: u64,
    bytes_descartados: u64,
    escritas_que_aguardaram: u64,
    leituras_que_aguardaram: u64,
    prazos_escrita_esgotados: u64,
    prazos_leitura_esgotados: u64,
}

impl Interior {
    fn novo() -> Self {
        Self {
            estado: EstadoDoFluxo::Aberto,
            blocos: VecDeque::new(),
            bytes_em_fila: 0,
            maior_ocupacao_bytes: 0,
            bytes_escritos: 0,
            bytes_lidos: 0,
            bytes_descartados: 0,
            escritas_que_aguardaram: 0,
            leituras_que_aguardaram: 0,
            prazos_escrita_esgotados: 0,
            prazos_leitura_esgotados: 0,
        }
    }

    fn estatisticas(&self) -> EstatisticasDoFluxo {
        EstatisticasDoFluxo {
            estado: self.estado,
            bytes_em_fila: self.bytes_em_fila,
            blocos_em_fila: self.blocos.len(),
            maior_ocupacao_bytes: self.maior_ocupacao_bytes,
            bytes_escritos: self.bytes_escritos,
            bytes_lidos: self.bytes_lidos,
            bytes_descartados: self.bytes_descartados,
            escritas_que_aguardaram: self.escritas_que_aguardaram,
            leituras_que_aguardaram: self.leituras_que_aguardaram,
            prazos_escrita_esgotados: self.prazos_escrita_esgotados,
            prazos_leitura_esgotados: self.prazos_leitura_esgotados,
        }
    }
}

/// Fila limitada de blocos de audio para um consumidor.
///
/// Multiplos produtores sao permitidos. Leitores concorrentes repartem os
/// blocos, nao recebem copias. Nao e um buffer de replay.
#[derive(Debug)]
pub struct FluxoDeAudio {
    configuracao: ConfiguracaoDoFluxo,
    interior: Mutex<Interior>,
    ha_dados: Condvar,
    ha_espaco: Condvar,
}

impl FluxoDeAudio {
    pub fn novo(configuracao: ConfiguracaoDoFluxo) -> Result<Self, ErroDoFluxo> {
        let configuracao = ConfiguracaoDoFluxo::nova(
            configuracao.capacidade_bytes,
            configuracao.maximo_blocos,
        )?;
        Ok(Self {
            configuracao,
            interior: Mutex::new(Interior::novo()),
            ha_dados: Condvar::new(),
            ha_espaco: Condvar::new(),
        })
    }

    fn travar(&self) -> MutexGuard<'_, Interior> {
        // Mesmo apos panico de outra thread, nao transformar a falha em
        // panico adicional no processo hospedeiro.
        self.interior.lock().unwrap_or_else(|erro| erro.into_inner())
    }

    /// Insere um bloco sem copia adicional, respeitando limites em bytes e blocos.
    ///
    /// Um tempo_maximo de zero permite somente insercao imediata.
    /// Um bloco maior que a capacidade e rejeitado imediatamente.
    /// Blocos vazios nao entram na fila.
    pub fn escrever_bloco(
        &self,
        bloco: Vec<u8>,
        tempo_maximo: Duration,
    ) -> Result<(), ErroDoFluxo> {
        let tamanho = bloco.len();
        if tamanho > self.configuracao.capacidade_bytes {
            return Err(ErroDoFluxo::BlocoMaiorQueCapacidade {
                bytes: tamanho,
                capacidade: self.configuracao.capacidade_bytes,
            });
        }

        let inicio = Instant::now();
        let mut aguardou = false;
        let mut interior = self.travar();

        loop {
            match interior.estado {
                EstadoDoFluxo::Cancelado => return Err(ErroDoFluxo::Cancelado),
                EstadoDoFluxo::Finalizado => return Err(ErroDoFluxo::Finalizado),
                EstadoDoFluxo::Aberto => {}
            }

            if tamanho == 0 {
                return Ok(());
            }

            let cabe_bytes = tamanho <= self.configuracao.capacidade_bytes - interior.bytes_em_fila;
            let cabe_bloco = interior.blocos.len() < self.configuracao.maximo_blocos;
            if cabe_bytes && cabe_bloco {
                interior.bytes_em_fila += tamanho;
                interior.maior_ocupacao_bytes = interior.maior_ocupacao_bytes.max(interior.bytes_em_fila);
                interior.bytes_escritos = interior.bytes_escritos.saturating_add(tamanho as u64);
                interior.blocos.push_back(bloco);
                drop(interior);
                self.ha_dados.notify_one();
                return Ok(());
            }

            if !aguardou {
                interior.escritas_que_aguardaram = interior.escritas_que_aguardaram.saturating_add(1);
                aguardou = true;
            }

            let restante = tempo_maximo.saturating_sub(inicio.elapsed());
            if restante.is_zero() {
                interior.prazos_escrita_esgotados = interior.prazos_escrita_esgotados.saturating_add(1);
                return Err(ErroDoFluxo::TempoEsgotado);
            }

            let (guarda, _) = self
                .ha_espaco
                .wait_timeout(interior, restante)
                .unwrap_or_else(|erro| erro.into_inner());
            interior = guarda;
        }
    }

    /// Retira um bloco. Ok(None) significa EOF normal depois de drenar a fila.
    /// Cancelamento e prazo esgotado sao erros distintos do EOF.
    pub fn ler_bloco(&self, tempo_maximo: Duration) -> Result<Option<Vec<u8>>, ErroDoFluxo> {
        let inicio = Instant::now();
        let mut aguardou = false;
        let mut interior = self.travar();

        loop {
            if interior.estado == EstadoDoFluxo::Cancelado {
                return Err(ErroDoFluxo::Cancelado);
            }

            if let Some(bloco) = interior.blocos.pop_front() {
                interior.bytes_em_fila -= bloco.len();
                interior.bytes_lidos = interior.bytes_lidos.saturating_add(bloco.len() as u64);
                drop(interior);
                self.ha_espaco.notify_all();
                return Ok(Some(bloco));
            }

            if interior.estado == EstadoDoFluxo::Finalizado {
                return Ok(None);
            }

            if !aguardou {
                interior.leituras_que_aguardaram = interior.leituras_que_aguardaram.saturating_add(1);
                aguardou = true;
            }
            let restante = tempo_maximo.saturating_sub(inicio.elapsed());
            if restante.is_zero() {
                interior.prazos_leitura_esgotados = interior.prazos_leitura_esgotados.saturating_add(1);
                return Err(ErroDoFluxo::TempoEsgotado);
            }

            let (guarda, _) = self
                .ha_dados
                .wait_timeout(interior, restante)
                .unwrap_or_else(|erro| erro.into_inner());
            interior = guarda;
        }
    }

    /// Nao aceita mais escrita; os blocos existentes continuam legiveis.
    /// Chamadas repetidas sao seguras.
    pub fn finalizar(&self) -> Result<(), ErroDoFluxo> {
        let mut interior = self.travar();
        match interior.estado {
            EstadoDoFluxo::Cancelado => return Err(ErroDoFluxo::Cancelado),
            EstadoDoFluxo::Finalizado => return Ok(()),
            EstadoDoFluxo::Aberto => interior.estado = EstadoDoFluxo::Finalizado,
        }
        drop(interior);
        self.ha_dados.notify_all();
        self.ha_espaco.notify_all();
        Ok(())
    }

    /// Descarta blocos pendentes e acorda todos os produtores e consumidores.
    /// Chamadas repetidas sao seguras. Cancelamento vence EOF.
    pub fn cancelar(&self) {
        let mut interior = self.travar();
        if interior.estado != EstadoDoFluxo::Cancelado {
            interior.estado = EstadoDoFluxo::Cancelado;
            interior.bytes_descartados = interior.bytes_descartados
                .saturating_add(interior.bytes_em_fila as u64);
            interior.blocos.clear();
            interior.bytes_em_fila = 0;
        }
        drop(interior);
        self.ha_dados.notify_all();
        self.ha_espaco.notify_all();
    }

    /// Snapshot pontual; nao faz trabalho em segundo plano.
    pub fn estatisticas(&self) -> EstatisticasDoFluxo {
        self.travar().estatisticas()
    }
}
