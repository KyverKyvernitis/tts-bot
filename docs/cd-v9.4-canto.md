# Osaka CD v9.4 — canto das mensagens com harmonias

Um CD selecionado, diferente de **Padrão**, passa a ser usado nas mensagens
com voz Teto/Vocaloid, mesmo sem `/cantar` ou `~`. A fala usa todas as partes
do trecho mostrado no painel, como a amostra. A voz principal canta a mensagem;
as harmonias e o baixo acompanham nas notas e vogais/letras MIDI do arranjo.
Um MIDI com apenas uma parte continua tendo uma voz musical.

A quantidade de notas do CD deixa de impor um mínimo ou máximo de sílabas
à mensagem. Palavras curtas continuam nas vogais, com novos ataques depois
das pausas. Frases maiores ganham articulações na mesma altura das notas;
se necessário, o trecho inteiro se repete, com todas as partes sincronizadas.
Palavras completas e sua ordem são preservadas. `~` continua dando peso à
sustentação; `<`, `>` e os limites de frase continuam sendo considerados.

A preparação usa o fonetizador original em uma cópia de leitura, separada
do worker, e não bloqueia o loop do bot. A busca de repetições é limitada e
usa expansão/busca binária. Os caches distinguem o canto adaptado da política
antiga; acompanhamentos iguais podem reutilizar seus áudios entre mensagens.

O painel mantém notas, capa, duração total, seletor e botões, sem duração de
cada nota. Cada nova chamada de `_cd` apaga o painel anterior e envia um novo,
como na v9.3. O catálogo e o MIDI original não são reescritos pela adaptação.

## Instalar na VPS

Aplique **osaka-cd-vps-correcao-v9.4.zip** pelo updater de código e aguarde a
conclusão. Esta correção é cumulativa para uma VPS já com a v9.1: inclui o
comportamento do painel da v9.3. Não contém `.env`, tokens, catálogos, bancos
de voz ou mudanças nos arquivos do Phone Worker 1.11.41.

Os CDs já publicados, incluindo Ghost Rule, não precisam ser reimportados.
A publicação automática já pareada pelo Tailscale continua funcionando.

## Instalar o ajuste no Termux

Baixe **osaka-cd-termux-ajuste-v9.4.zip** para Downloads. Na sessão que está
rodando `--watch`, pressione **Ctrl+C** antes de extrair. Execute:

```sh
unzip -o "$HOME/storage/downloads/osaka-cd-termux-ajuste-v9.4.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v9.1"
python deploy/ttv-cd/configure-cd.py --check
python deploy/ttv-cd/import-midi.py --watch
```

O ajuste usa a pasta existente **osaka-cd-termux-kit-v9.1**. Não é necessário
gerar chaves, copiar o token novamente ou reinstalar o Phone Worker. Ele
atualiza apenas a biblioteca compartilhada de composição e acrescenta este
guia. O importador pode continuar identificando sua lógica como versão 9.0;
este pacote corrige a execução vocal, não muda o protocolo de importação.

## Conferir no Discord

Abra `_cd`, selecione **Ghost Rule**, toque em **Usar esse CD** e use a voz
Teto/Vocaloid em uma call. Com o prefixo pessoal atual `'`, teste:

```text
'porra
'eu vou dormir agora amor e te ligo mais tarde pra você fazer um sistema para eu te mandar
```

As mensagens devem cantar com o acompanhamento do CD, sem a recusa por
quantidade de sílabas mostrada na captura. Selecione **Padrão** para voltar
à fala comum; nele, `/cantar` ou `~` separado ainda ativa o canto explícito.

Os limites físicos de execução continuam: texto até 2.048 caracteres,
até três partes, pedidos individuais de até 128 notas e limites de duração,
memória e transporte. Eles são tratados pela composição e não exigem contar
sílabas manualmente. Letras ou palavras extremas ainda podem ultrapassar esses
limites; eles não foram removidos do backend.

## Validação e alcance

Os testes executam o alinhador e o validador originais do worker, sem alterar
seus arquivos. Cobrem palavras curtas e frases longas em todos os CDs embutidos,
pausas, sustentações, português/inglês, limites de segmentos e caches. O fluxo
de preparação/fila é executado com transporte simulado. A mistura é verificada
com áudio PCM de três frequências e MP3 decodificado por FFmpeg.

O teste com frequências valida a presença e sincronização das partes; não
comprova a qualidade de uma voicebank UTAU. A voz real, a entrega em call e o
Discord precisam ser conferidos após instalar, no telefone/VPS do usuário.
