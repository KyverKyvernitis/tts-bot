use std::sync::Arc;
use std::thread;
use std::time::Duration;

use nucleo_fluxo_tts::{ConfiguracaoDoFluxo, ErroDoFluxo, EstadoDoFluxo, FluxoDeAudio};

const IMEDIATO: Duration = Duration::from_millis(0);
const ESPERA: Duration = Duration::from_secs(2);

fn fluxo(capacidade: usize, blocos: usize) -> FluxoDeAudio {
    FluxoDeAudio::novo(ConfiguracaoDoFluxo::nova(capacidade, blocos).unwrap()).unwrap()
}

#[test]
fn respeita_limite_de_bytes_e_de_blocos() {
    let fluxo = fluxo(8, 2);
    fluxo.escrever_bloco(vec![1, 2, 3, 4], IMEDIATO).unwrap();
    fluxo.escrever_bloco(vec![5, 6, 7, 8], IMEDIATO).unwrap();
    assert_eq!(fluxo.escrever_bloco(vec![9], IMEDIATO), Err(ErroDoFluxo::TempoEsgotado));
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), Some(vec![1, 2, 3, 4]));
    fluxo.escrever_bloco(vec![9], IMEDIATO).unwrap();
    let estado = fluxo.estatisticas();
    assert_eq!(estado.bytes_em_fila, 5);
    assert_eq!(estado.blocos_em_fila, 2);
    assert_eq!(estado.maior_ocupacao_bytes, 8);
    assert_eq!(estado.prazos_escrita_esgotados, 1);
}

#[test]
fn limite_de_blocos_independe_do_limite_de_bytes() {
    let fluxo = fluxo(100, 1);
    fluxo.escrever_bloco(vec![1], IMEDIATO).unwrap();
    assert_eq!(fluxo.escrever_bloco(vec![2], IMEDIATO), Err(ErroDoFluxo::TempoEsgotado));
    assert_eq!(fluxo.estatisticas().bytes_em_fila, 1);
}

#[test]
fn finalizacao_entrega_blocos_antes_de_eof() {
    let fluxo = fluxo(10, 3);
    fluxo.escrever_bloco(vec![1, 2], IMEDIATO).unwrap();
    fluxo.escrever_bloco(vec![3], IMEDIATO).unwrap();
    fluxo.finalizar().unwrap();
    fluxo.finalizar().unwrap();
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), Some(vec![1, 2]));
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), Some(vec![3]));
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), None);
    assert_eq!(fluxo.escrever_bloco(vec![4], IMEDIATO), Err(ErroDoFluxo::Finalizado));
    assert_eq!(fluxo.estatisticas().estado, EstadoDoFluxo::Finalizado);
}

#[test]
fn cancelamento_descarta_bloco_e_diferencia_de_eof() {
    let fluxo = fluxo(5, 1);
    fluxo.escrever_bloco(vec![7, 8], IMEDIATO).unwrap();
    fluxo.cancelar();
    fluxo.cancelar();
    assert_eq!(fluxo.ler_bloco(IMEDIATO), Err(ErroDoFluxo::Cancelado));
    assert_eq!(fluxo.escrever_bloco(vec![1], IMEDIATO), Err(ErroDoFluxo::Cancelado));
    assert_eq!(fluxo.finalizar(), Err(ErroDoFluxo::Cancelado));
    let estado = fluxo.estatisticas();
    assert_eq!(estado.estado, EstadoDoFluxo::Cancelado);
    assert_eq!(estado.bytes_descartados, 2);
    assert_eq!(estado.bytes_em_fila, 0);
}

#[test]
fn bloco_maior_que_buffer_nao_bloqueia() {
    let fluxo = fluxo(2, 1);
    assert_eq!(fluxo.escrever_bloco(vec![1, 2, 3], ESPERA), Err(
        ErroDoFluxo::BlocoMaiorQueCapacidade { bytes: 3, capacidade: 2 }
    ));
}

#[test]
fn operacoes_vazias_e_configuracao_invalida() {
    assert_eq!(ConfiguracaoDoFluxo::nova(0, 1), Err(ErroDoFluxo::ConfiguracaoInvalida));
    assert_eq!(ConfiguracaoDoFluxo::nova(1, 0), Err(ErroDoFluxo::ConfiguracaoInvalida));
    let fluxo = fluxo(8, 1);
    fluxo.escrever_bloco(Vec::new(), IMEDIATO).unwrap();
    assert_eq!(fluxo.estatisticas().blocos_em_fila, 0);
    assert_eq!(fluxo.ler_bloco(IMEDIATO), Err(ErroDoFluxo::TempoEsgotado));
    assert_eq!(fluxo.estatisticas().prazos_leitura_esgotados, 1);
}

#[test]
fn produtor_e_consumidor_concorrentes_preservam_ordem() {
    let fluxo = Arc::new(fluxo(64, 2));
    let produtor = Arc::clone(&fluxo);
    let tarefa = thread::spawn(move || {
        for i in 0..1000_u32 {
            produtor.escrever_bloco(i.to_le_bytes().to_vec(), ESPERA).unwrap();
        }
        produtor.finalizar().unwrap();
    });
    for i in 0..1000_u32 {
        let bloco = fluxo.ler_bloco(ESPERA).unwrap().unwrap();
        assert_eq!(bloco, i.to_le_bytes().to_vec());
    }
    assert_eq!(fluxo.ler_bloco(ESPERA).unwrap(), None);
    tarefa.join().unwrap();
    let estado = fluxo.estatisticas();
    assert_eq!(estado.bytes_escritos, 4000);
    assert_eq!(estado.bytes_lidos, 4000);
    assert!(estado.maior_ocupacao_bytes <= 64);
}

#[test]
fn cancelamento_desbloqueia_produtor() {
    let fluxo = Arc::new(fluxo(1, 1));
    fluxo.escrever_bloco(vec![1], IMEDIATO).unwrap();
    let clone = Arc::clone(&fluxo);
    let tarefa = thread::spawn(move || clone.escrever_bloco(vec![2], ESPERA));
    fluxo.cancelar();
    assert_eq!(tarefa.join().unwrap(), Err(ErroDoFluxo::Cancelado));
}

#[test]
fn finalizacao_desbloqueia_leitor_em_espera() {
    let fluxo = Arc::new(fluxo(1, 1));
    let clone = Arc::clone(&fluxo);
    let tarefa = thread::spawn(move || clone.ler_bloco(ESPERA));
    fluxo.finalizar().unwrap();
    assert_eq!(tarefa.join().unwrap(), Ok(None));
}

#[test]
fn finalizar_desbloqueia_produtor_cheio() {
    let fluxo = Arc::new(fluxo(1, 1));
    fluxo.escrever_bloco(vec![1], IMEDIATO).unwrap();
    let clone = Arc::clone(&fluxo);
    let tarefa = thread::spawn(move || clone.escrever_bloco(vec![2], ESPERA));
    fluxo.finalizar().unwrap();
    assert_eq!(tarefa.join().unwrap(), Err(ErroDoFluxo::Finalizado));
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), Some(vec![1]));
}

#[test]
fn cancelamento_desbloqueia_leitor_sem_gerar_eof() {
    let fluxo = Arc::new(fluxo(1, 1));
    let clone = Arc::clone(&fluxo);
    let tarefa = thread::spawn(move || clone.ler_bloco(ESPERA));
    fluxo.cancelar();
    assert_eq!(tarefa.join().unwrap(), Err(ErroDoFluxo::Cancelado));
}

#[test]
fn varios_produtores_respeitam_capacidade_e_preservam_total() {
    let fluxo = Arc::new(fluxo(32, 4));
    let produtores: Vec<_> = (0..4).map(|identificador| {
        let clone = Arc::clone(&fluxo);
        thread::spawn(move || {
            for _ in 0..100 {
                clone.escrever_bloco(vec![identificador; 8], ESPERA).unwrap();
            }
        })
    }).collect();
    let mut total = 0_usize;
    while total < 4 * 100 * 8 {
        let bloco = fluxo.ler_bloco(ESPERA).unwrap().unwrap();
        assert_eq!(bloco.len(), 8);
        total += bloco.len();
    }
    for produtor in produtores { produtor.join().unwrap(); }
    fluxo.finalizar().unwrap();
    assert_eq!(fluxo.ler_bloco(IMEDIATO).unwrap(), None);
    let estado = fluxo.estatisticas();
    assert_eq!(estado.bytes_lidos, total as u64);
    assert!(estado.maior_ocupacao_bytes <= 32);
}
