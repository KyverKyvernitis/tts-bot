# Osaka CD v9.5 — terminar o canto quando a letra termina

A v9.4 completava as notas restantes do CD com a última vogal. A v9.5 remove
esse preenchimento: cada sílaba ocupa uma nota, e cada `~` acrescenta uma
nota de sustentação. O canto termina na última nota usada pela mensagem.

```text
'a          → uma nota
'a ~        → duas notas
'a ~ ~      → três notas
'porra      → duas notas, conforme o fonetizador português
```

As harmonias e o baixo são cortados no mesmo instante da última nota da voz
principal. Notas que atravessam esse limite têm sua duração reduzida; uma voz
que começa depois do fim da frase não toca. A duração da mistura também é
reduzida, para não enviar o restante da música como silêncio.

As palavras continuam completas e na ordem da mensagem. Letras maiores podem
usar mais notas e repetir o trecho disponível quando necessário. O tom, as
durações das notas e o tempo do CD são preservados. Não há mais sustentações
automáticas para preencher o restante do trecho. `a~` também solicita uma
nota adicional; com espaço, `a ~` deixa a contagem mais explícita.

Uma sustentação que atravessa uma pausa MIDI reinicia a mesma vogal na nota
seguinte, conforme o limite do worker. A divisão física de uma nota muito
longa não acrescenta notas musicais ao CD. Os limites físicos de texto,
fonemas, tempo e recursos do backend continuam vigentes.

O painel, os CDs publicados, o pareamento Tailscale e os arquivos do Phone
Worker permanecem os mesmos. A amostra continua apresentando o trecho do CD;
a fala usa apenas as notas necessárias para sua mensagem. A nova política
de execução separa o cache das falas prolongadas da v9.4.

## Instalar na VPS

Aplique **osaka-cd-vps-correcao-v9.5.zip** pelo updater de código e aguarde a
conclusão. Este ajuste é para a **v9.4 já instalada**. Não precisa reimportar
Ghost Rule ou publicar seus CDs novamente.

## Instalar o ajuste no Termux

Baixe **osaka-cd-termux-ajuste-v9.5.zip** para Downloads. Pare a sessão antiga
do importador com **Ctrl+C**, antes de extrair. Execute:

```sh
unzip -o "$HOME/storage/downloads/osaka-cd-termux-ajuste-v9.5.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v9.1"
python deploy/ttv-cd/configure-cd.py --check
python deploy/ttv-cd/import-midi.py --watch
```

A pasta continua sendo **osaka-cd-termux-kit-v9.1**. O ajuste atualiza a
biblioteca compartilhada; não exige instalar dependências, refazer chaves ou
copiar tokens. Deixe apenas uma sessão do `--watch` em execução.

## Conferir

Selecione o CD em `_cd`, toque em **Usar esse CD** e envie `'a ~` na call.
Devem tocar as duas primeiras notas do trecho, com o acompanhamento encerrando
junto. Em seguida compare com `'a ~ ~`, que deve usar três notas.

Os testes usam o alinhador e o validador originais do worker, transporte
simulado e mistura PCM decodificada por FFmpeg. Eles verificam a contagem,
as pausas, os efeitos, as frases maiores, o corte das harmonias e a duração
do áudio. A voz UTAU real e a entrega no Discord ainda precisam ser ouvidas
no telefone após a instalação.
