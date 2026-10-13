# Painel de canto

Use `_cd` para abrir suas melodias. O comando usa o prefixo do bot configurado
no servidor: com prefixo `!`, por exemplo, o comando é `!cd`.

O painel usa Components V2 e começa com **Padrão** e dez trechos de músicas:
World Is Mine, Senbonzakura, Melt, Tell Your World, PoPiPo, Mesmerizer,
Ochame Kinou (Fukkireta), Override, Teto Territory e Triple Baka.

**Padrão** usa C4, E4, G4 e E4, em um ciclo de quatro segundos,
e mostra o avatar atual do bot. As músicas têm partituras próprias, com notas,
pausas e andamento do trecho identificado no catálogo. O painel mostra a
quantidade de notas e a duração para ajudar a escolher uma melodia que comporte
a letra. As fontes das transcrições estão em `cogs/tts/cd_song_scores.json`.

**Ouvir amostra** envia um MP3 privado, cantado pela vocaloid e no idioma
selecionados no TTV. As músicas são demonstradas com “lá” em cada nota; não
incluem a letra original. A amostra usa as notas e o andamento da melodia, sem
alterar os ajustes de tom e velocidade da fala. Ela não entra na fila nem
conecta o bot à call. Uma amostra pronta e compatível toca mesmo com o worker
offline; gerar outra amostra exige o worker disponível.

**Usar esse CD** confirma a seleção pessoal neste servidor. A navegação pelo
seletor não salva ajustes. Uma opção já em uso aparece confirmada; a escolha
permanece após reiniciar o bot. Abrir, selecionar e salvar continuam disponíveis
com o worker offline. Somente quem abriu o painel pode usar seus controles.

O canto com `'/cantar sua letra` ou `~` separado usa a melodia pessoal. Anexos MIDI enviados pelo usuário não alteram a partitura do canto. A fala comum mantém
seu comportamento, e a melodia do canto que já entrou na fila permanece igual.
Cada trecho comporta até uma sílaba por nota; com menos sílabas a voz pode
prolongar as vogais nas notas seguintes, respeitando as pausas. Para uma letra
maior, escolha um trecho com mais notas ou divida a mensagem. Uma letra curta
precisa ter ataques vocais suficientes para as frases separadas por pausas:
uma única vogal não pode atravessar uma pausa MIDI. O bot informa a
incompatibilidade em vez de tratar esse caso como voz desativada.

O painel aberto pelo comando expira após cinco minutos sem interação autorizada;
use `_cd` novamente. Reabrir no mesmo canal reutiliza seu painel ainda ativo;
um painel expirado é substituído. A atualização automática do catálogo ou dos
emojis não prolonga a validade. O bot conserva o despacho dos controles por até
24 horas para responder à interação expirada com o mesmo aviso privado de
`_tts`, trocando o comando para `_cd`. Esta mudança mantém o
runtime do worker 1.11.41. As partituras são enviadas pelo bot ao worker, sem
precisar anexar um MIDI nem trocar o motor instalado.

O painel dá preferência às capas anexadas do cache local. Até preparar esse
cache, usa a imagem pública do vídeo indicado no catálogo. Abrir e selecionar
uma melodia não pesquisa vídeos no YouTube. O preparo de thumbnails para o Termux fica em
`deploy/ttv-cd/`; ele reaproveita a conversão e a validação de capas da função de
música, e mantém o cache fora das releases do worker.

A seleção fica no documento de preferências TTS do usuário (`ttv_melody_id`).
O roteamento `_cd` usa o prefixo do bot configurado no servidor.

## Menu de CDs: capas no seletor

O painel usa emojis estáticos da própria aplicação Discord. Ao iniciar e quando
o catálogo muda, o Osaka verifica os emojis existentes e cria os que faltam se
as imagens JPEG locais estiverem presentes no pacote de capas. Usa o método
`create_application_emoji`, disponível em discord.py >= 2.5. Os nomes são
determinísticos, então a mesma imagem é reutilizada em todos os servidores e
após reiniciar o bot. Nenhum emoji é criado por usuário/servidor.

A sincronização ocorre em segundo plano e não bloqueia a abertura do menu.
Se o Discord rejeitar a criação ou a imagem não estiver instalada, o seletor
continua funcionando apenas com o nome da música. A criação não é repetida
a cada clique: tentativas são limitadas e emojis já existentes são reaproveitados.
Cada música recupera suas falhas independentemente: uma imagem inválida ou
rejeição específica não bloqueia as seguintes. Falhas temporárias recebem
novas tentativas sem abrir o painel novamente; limites e bloqueios globais
respeitam cooldown. Uma criação cuja resposta se perdeu verifica o registro
remoto antes de tentar novamente, evitando duplicatas. Não se apagam emojis
para contornar a cota da aplicação.
O painel principal mantém a imagem em tamanho maior; no seletor o tamanho da
miniatura é limitado pelo tamanho do emoji nativo do Discord.

Arquivos locais aprovados por SHA-256 e formato são reutilizados em cache
limitado. A troca do arquivo, do hash ou da partitura invalida somente os
artefatos correspondentes. Editar um painel com a mesma capa reutiliza seu
anexo, sem novo upload; a criação dos emojis não consulta o YouTube.

Uma thumbnail visível não comprova que o JPEG local está instalado: o painel
também aceita a URL pública, mas criar o emoji requer os bytes locais validados.
Para conferir sem credenciais, execute da raiz do projeto:

```bash
python3 scripts/diagnose-cd-assets.py rolling-girl
sudo journalctl --since "2 hours ago" --no-pager | grep -F '[cd_emoji]'
```

O diagnóstico verifica o hash e a conversão real em JPEG 128×128. Os logs
registram estado, tipo do erro, status HTTP e código Discord, sem tokens.

Não há configuração secreta adicional e nem alterações no Phone Worker.
O catálogo inicial permanece disponível. Novos CDs são incorporados por ZIPs versionados do kit Termux.

## Informações das partituras e botão de estado

A linha de detalhes mostra a quantidade de notas e a duração; **logo abaixo**
mostra as notas da partitura na sequência original, incluindo sustenidos e
as oitavas (exemplo do Padrão: `C4 · E4 · G4 · E4`). A conversão parte dos
valores MIDI do catálogo, sem criar ou alterar notas de síntese.

O botão ativo utiliza o emoji `circulando` e o texto **Usar esse CD**.
Quando a melodia já está selecionada para o usuário e servidor, aparece
**CD já em uso** com o emoji `Vinyl_Music_loading` e o botão fica desativado.

A opção **Padrão** ganha como emoji a foto de perfil atual do bot. O mesmo
registro de emojis da aplicação é reutilizado nos servidores. O hash da
foto identifica cada versão e permite atualizar o emoji quando ela mudar;
a imagem é obtida somente no processo assíncrono de sincronização.
A falta de acesso à imagem ou o limite de emojis não bloqueia o painel.

## Importação incremental de CDs (VPS + Termux)

Use o Termux Kit v8 com MIDIs locais. O novo extrator aceita arranjos polifônicos,
seleciona faixa/canal e um trecho de até 60 segundos/128 notas, conservando
onsets, pausas e andamento. Extrações heurísticas registram ambiguidade vocal;
o arquivo original não é alterado. Nenhum MIDI é recebido pelo Discord.

O mecanismo de metadados reutiliza as fontes do player, procura aliases em
múltiplas consultas limitadas e confere título, artista, versão e visualizações.
O relatório explica os candidatos rejeitados e a origem/data das contagens.
A escolha é a maior entre os candidatos identificados inspecionados.

Cada ZIP exporta somente um CD, seus índices e artefatos concluídos. O updater
mescla registros por ID e valida a mesma estrutura usada pelo kit, incluindo
SHA-256, JPEG e decodificação do MP3, novamente antes de promover o worktree.
Partitura e MIDI têm hashes separados. Mudanças musicais exigem nova revisão e
invalidam a amostra; trocar a capa preserva o canto e invalida emoji/cor.
Os artefatos pendentes ficam no manifesto e os prontos são conservados.

O importador carrega o arquivo confiável `~/.phone-worker.env` em um processo
separado. Usa o renderizador UTAU da release instalada 1.11.41, sem atualizar,
reiniciar ou escrever no runtime. Só configura o cache isolado do importador.
Respeita bateria/temperatura; falhas transitórias recebem backoff e entradas
inválidas aguardam mudança. `--retry` permite nova tentativa manual imediata.

Após instalar o patch de código da VPS, aplique o ZIP da música pelo updater.
A publicação HTTP opcional está em `/api/osaka/cd/publish`, desativada por
padrão e autenticada com token separado. Aceitar/enfileirar o pacote não
comprova instalação. Consulte `deploy/ttv-cd/INSTRUCOES-IMPORTACAO.md` e
`updater/README.md` para a configuração e a verificação da aplicação.

O catálogo e painéis ativos detectam a atualização em até 15 segundos. O menu
pagina a cada 25 opções; painéis expirados/encerrados não são reativados. O
emoji usa o hash da capa, e a cor vem do JPEG local validado. Amostras Teto
pt-BR prontas podem tocar offline com tom musical 0 e velocidade 1, mesmo
quando as preferências de fala são diferentes.

## Recuperação do painel e dos erros — v8.3

Aberturas do mesmo usuário são agrupadas; operações de configuração, envio e
edição têm prazo. Uma exclusão lenta da mensagem anterior não impede o registro
da mensagem nova já enviada. Falhas liberam o estado de abertura para nova
tentativa, sem reativar um painel que expirou durante a espera. As interações
de seleção/amostra são reconhecidas antes de operações que possam demorar.

Rejeições explícitas do worker por letra, partitura, recursos ou configuração
de uma voz não desativam a rota inteira. Erros de rede/autenticação e falhas
não identificadas conservam a proteção de disponibilidade. O backend UTAU
1.11.41, voicebank e resampler permanecem preservados.

Execute o diagnóstico com o Python usado pelo serviço do bot. O script informa
o executável, a disponibilidade de Pillow e os candidatos de capa com caminho,
hash e motivo de rejeição. A falta de Pillow nesse processo retorna estado
indeterminado quando os bytes e o hash conferem, sem declarar a capa ausente.
