/// Aberto: aceita dados; Finalizado: drena os blocos e depois entrega EOF;
/// Cancelado: descarta os blocos e interrompe leitores e escritores.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EstadoDoFluxo {
    Aberto,
    Finalizado,
    Cancelado,
}

/// Fotografia dos contadores, obtida sem rede nem acesso ao disco.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EstatisticasDoFluxo {
    pub estado: EstadoDoFluxo,
    pub bytes_em_fila: usize,
    pub blocos_em_fila: usize,
    pub maior_ocupacao_bytes: usize,
    pub bytes_escritos: u64,
    pub bytes_lidos: u64,
    pub bytes_descartados: u64,
    pub escritas_que_aguardaram: u64,
    pub leituras_que_aguardaram: u64,
    pub prazos_escrita_esgotados: u64,
    pub prazos_leitura_esgotados: u64,
}
