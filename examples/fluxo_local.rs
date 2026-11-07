use std::sync::Arc;
use std::thread;
use std::time::Duration;

use nucleo_fluxo_tts::{ConfiguracaoDoFluxo, FluxoDeAudio};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let fluxo = Arc::new(FluxoDeAudio::novo(ConfiguracaoDoFluxo::default())?);
    let produtor = Arc::clone(&fluxo);

    let tarefa = thread::spawn(move || -> Result<(), nucleo_fluxo_tts::ErroDoFluxo> {
        for numero in 0_u8..4 {
            produtor.escrever_bloco(vec![numero; 1024], Duration::from_secs(2))?;
        }
        produtor.finalizar()?;
        Ok(())
    });

    let mut total = 0_usize;
    while let Some(bloco) = fluxo.ler_bloco(Duration::from_secs(2))? {
        total += bloco.len();
    }
    tarefa.join().expect("produtor interrompido")?;

    println!("Bytes consumidos: {total}");
    println!("Estatisticas: {:?}", fluxo.estatisticas());
    Ok(())
}
