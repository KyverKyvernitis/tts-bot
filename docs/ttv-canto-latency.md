# Desempenho do canto `_cd`

O objetivo deste ajuste é reduzir a espera por letras inéditas no telefone,
preservando voz, notas, duração e harmonias. Os ganhos precisam ser medidos no
aparelho com as voicebanks instaladas; testes com tons ou resampler de teste não
comprovam a velocidade nem a qualidade do ESPER no Android.

## Mudanças

- O cache de fragmentos passa a contabilizar suas gravações sem percorrer todos
  os WAVs a cada fragmento. A manutenção reconcilia o conteúdo em lotes e mantém
  os fragmentos usados pela renderização protegidos até terminar a composição.
- A curva de intensidade MIDI calcula a transição de 8 ms e reutiliza o ganho
  constante no restante da nota. A comparação de PCM verifica que o resultado
  mantém os mesmos arredondamentos.
- A composição e o transporte anunciam tempos por etapa e contadores do renderer.
  Isso permite distinguir espera, síntese física, reaproveitamento de cache,
  mixagem e codificação. Contadores de síntese de um pedido antigo não devem ser
  apresentados como trabalho executado em uma resposta de cache.
- Ao confirmar **Usar esse CD**, o bot pode preparar acompanhamentos fixos em
  segundo plano, durante a ociosidade, para reaproveitá-los nas próximas letras.

O cache de análise do ESPER e os caches de fragmentos, partes e mixagem já
existiam. A implementação conserva as identidades que separam voz, idioma,
partitura, efeitos e fingerprint real do banco/motor.

## Aquecimento dos acompanhamentos

Confirmar **Usar esse CD** agenda apenas harmonias e baixo cujas notas e letras
independem da mensagem. O bot não antecipa a voz principal, uma letra de usuário
ou a mixagem final. O pedido aguarda cinco segundos de ociosidade; se não houver
oportunidade em até cinco minutos, ele é descartado. Cada tarefa tem limite de
oito solicitações de partes/trechos e orçamento total de cinco minutos.

Uma mensagem real tem prioridade: o worker interrompe cooperativamente o
aquecimento e espera o encerramento físico do processo antes de liberar seus
recursos para a mensagem. Os slots de requisição de primeiro plano e de
aquecimento são separados. A troca de prioridade não entrega áudio parcial.

O aquecimento só é admitido quando o worker anuncia
`ttv_background_protocol=1` e o renderer reconhece uma ponte ESPER atualizada,
com assinatura de implementação verificada e cancelamento cooperativo.
Workers antigos e resamplers personalizados sem essa garantia continuam
atendendo mensagens normalmente e recusam o aquecimento.

O planejamento e as chaves de cache são os mesmos da composição real. Isso
preserva a separação por voz, idioma, partitura e fingerprint. A adaptação da
letra pode encurtar o último trecho; se a entrada resultante for diferente,
esse trecho ainda precisa ser sintetizado. Um acompanhamento pré-renderizado
não substitui notas ou duração diferentes.

O recurso vem habilitado no bot. Para desativá-lo, configure
`OSAKA_CD_WARMUP_ENABLED=false` no ambiente da VPS e reinicie o bot.

## Medir o canto real no Termux

Execute o benchmark a partir da cópia atualizada do repositório no telefone,
com o worker ocioso. Ele exige Python, FFmpeg, ESPER e a voicebank English-CVVC
da personagem. Leia a configuração instalada com `--env`; o arquivo não é
executado como shell e apenas parâmetros de áudio são retidos.

```sh
python scripts/benchmark-ttv-cd.py \
  --cd ttv-demo-v1 --voice teto --rounds 1 --threads 1 2 \
  --text 'Olá meu amor' --new-text 'Eu vou cantar' \
  --output "$HOME/storage/downloads/ttv-cd-desempenho.json"
```

Para a Miku, use `--voice miku --threads 1 2 4`. Se necessário, acrescente
`--env "$HOME/.phone-worker.env"`, `--voicebank CAMINHO`,
`--container voicepeak-arm64` e `--esper-root "$HOME/.esper-utau"`.
`--language en-US` mede a leitura da letra em inglês. Os parâmetros de tom e
velocidade do canto permanecem em zero e um, conforme o contrato de partitura.

`--cd` consulta o catálogo disponível nessa cópia/runtime. Para um CD importado
que só existe na VPS, forneça uma cópia do JSON de arranjo com
`--arrangement /caminho/arranjo.json`. O arquivo é apenas lido. Teste também
um arranjo com três partes e frases mais longas para representar o uso real.

O benchmark cria caches temporários separados para análise ESPER, fragmentos,
partes e mixagem. Mesmo um `--cache-dir` salvo no comando do resampler é
substituído apenas no perfil temporário. Configuração, voicebanks e caches de
produção são preservados. O JSON é o único artefato mantido quando `--output`
é informado; não são publicados WAVs nem mensagens.

## Interpretar o relatório

Há uma execução inicial `analysis_cold`, com análise temporária vazia. Em cada
rodada/configuração de threads são executados estes cenários:

| Fase | O que mede |
| --- | --- |
| `analysis_warm_new_fragments` | Mesma letra, análise anterior disponível e caches de fragmentos/partes vazios. |
| `exact_repeat` | Mesma letra, com as partes e a mixagem já prontas. |
| `new_text_same_cd` | Outra letra no mesmo CD, conservando os caches e reaproveitando acompanhamentos quando suas entradas continuam iguais. |

As rodadas alternam a ordem das configurações de threads. A análise aquecida
cobre as gravações usadas anteriormente; uma palavra nova pode exigir outras
gravações. `analysis_cold` não significa cache de arquivos do sistema operacional
vazio. Cada fase inclui adaptação da letra e composição local, sem HTTP, fila do
bot ou entrega ao Discord.

- `elapsed_ms`: tempo local da fase, incluindo preparação, identidade do banco e
  composição. `prepare_ms`, `profile_ms` e `render_ms` separam esses custos.
- `composition_metrics`: tempos da composição em `timing_ms`, incluindo consulta
  aos caches, síntese física, mixagem e codificação.
- `physical_parts`: somente chamadas físicas desta fase, com duração e métricas
  como `resample_ms`, `resampler_calls`, fragmentos novos/reutilizados e workers
  efetivos. Um acerto de cache de parte não gera uma chamada física.
- `fixed_stem_cache_hits` e `mix_cache_hit`: distinguem o reaproveitamento dos
  acompanhamentos do reaproveitamento da mensagem completa.
- `synthesis_to_audio_ratio`: tempo de composição dividido pela duração final.
  Um valor 2 indica dois segundos de composição por segundo de áudio.
- `pcm_matches_reference`: compara as amostras PCM de cada parte para entradas
  iguais entre cenários/configurações, antes da codificação MP3. Uma divergência
  encerra a comparação com erro.
- `temperature_before_c`/`temperature_after_c`: temperatura informada pela API
  Termux quando disponível; `null` quando indisponível.

O resumo contém mediana (`p50`) e `p95`. Com uma ou poucas rodadas, esses números
são apenas exploratórios. Compare antes/depois no mesmo aparelho, CD, letra,
configuração e condições térmicas. O tempo até começar a tocar no Discord deve
ser avaliado também pela telemetria do bot, pois este benchmark é local.

## Atualização e limites

O ZIP completo deve ser aplicado pelo updater da VPS. A automação existente
publica uma release do Phone Worker com os módulos de renderer e cache; o
bootstrap já pareado precisa baixar, validar e promover essa release no
telefone. Instalações manuais exigem atualizar também os arquivos do worker
no Termux. A versão anunciada e o hash da release devem corresponder ao código
atualizado antes de avaliar as mudanças do renderer.

A ponte instalada em `~/.esper-utau/bin` pertence ao kit ESPER separado e não
faz parte da release automática do Phone Worker. O setup atualizado reconhece
a ponte anterior distribuída por este repositório, cria backup e publica a
versão com cancelamento cooperativo. Uma ponte personalizada é preservada.
Os comandos salvos que contêm `--implementation-id` também precisam apontar
para a identidade da nova ponte; não substitua somente o arquivo Python
manualmente, pois uma identidade antiga faz a validação recusar a síntese.
Atualizar apenas o bot não habilita o aquecimento em uma ponte antiga.

Para atualizar a ponte, extraia o ZIP completo em uma pasta própria no telefone
e entre na raiz desse repositório. Pare o worker pelo mesmo mecanismo usado
normalmente na instalação: encerre a sessão/watch ou supervisor que o inicia,
para ele não reiniciar durante a atualização. Com o worker parado, execute:

```sh
python deploy/esper-utau/termux/setup.py --container voicepeak-arm64
python deploy/esper-utau/termux/refresh-bridge-config.py --check
python deploy/esper-utau/termux/refresh-bridge-config.py
```

Continue para o próximo comando somente quando o anterior retornar `ok: true`.
O setup reutiliza os binários instalados quando seus hashes conferem. O comando
com `--check` mostra se existem identidades antigas a atualizar, sem gravar.
O seguinte faz backup e atualiza somente as identidades conhecidas da ponte
nos comandos já existentes da Teto e da Miku, preservando flags e os demais
ajustes. Comandos personalizados são preservados e não são migrados.

Se sua instalação usa outro local, passe `--root /caminho/esper` ao setup e
`--esper-root /caminho/esper` às duas chamadas de `refresh-bridge-config.py`.
Para um arquivo de configuração diferente, acrescente
`--env /caminho/worker.env` às duas chamadas de atualização de identidade.
Use o nome do container já instalado no argumento `--container`; não crie
outro ambiente apenas para este ajuste.

Reinicie o worker pelo launcher/supervisor que já utiliza
(`~/phone-worker/start-phone-worker.sh` nas instalações padrão). Não execute
`activate-esper.py` como atalho para esta migração: a ativação altera outros
controles da Teto e não atualiza a identidade da Miku. Este procedimento não
troca a voicebank nem exige reinstalar o motor ESPER.

O benchmark deve acompanhar o conjunto `scripts`, `cogs/tts`, `osaka_cd` e
`deploy` desta revisão para usar as mesmas bibliotecas de preparação e síntese.

O paralelismo permanece limitado a dois renders para Teto e quatro para Miku.
Mais conexões HTTP não equivalem a mais capacidade de síntese no telefone.
Esta rodada conserva o ESPER atual: a execução persistente/em lote exige medir
se a partida do PRoot/.NET domina o custo e integrar uma API que efetivamente
evite reiniciar o motor por fragmento.
