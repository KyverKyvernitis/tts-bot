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
conecta o bot à call. O worker de síntese precisa estar disponível para ouvir.

**Usar esse CD** confirma a seleção pessoal neste servidor. A navegação pelo
seletor não salva ajustes. Uma opção já em uso aparece confirmada; a escolha
permanece após reiniciar o bot. Abrir, selecionar e salvar continuam disponíveis
com o worker offline. Somente quem abriu o painel pode usar seus controles.

O canto com `'/cantar sua letra` ou `~` separado usa a melodia pessoal. Anexos MIDI enviados pelo usuário não alteram a partitura do canto. A fala comum mantém
seu comportamento, e a melodia do canto que já entrou na fila permanece igual.
Cada trecho comporta até uma sílaba por nota; com menos sílabas a voz pode
prolongar as vogais nas notas seguintes, respeitando as pausas. Para uma letra
maior, escolha um trecho com mais notas ou divida a mensagem.

O painel expira após 24 horas sem interação; use `_cd` novamente. Reabrir substitui
somente o seu próprio painel de canto nesse servidor. Esta mudança mantém o
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

O painel usa emojis estáticos da própria aplicação Discord. No primeiro acesso,
o Osaka verifica os emojis existentes e cria os que ainda faltam **somente** se
as imagens JPEG locais estiverem presentes no pacote de capas. Usa o método
`create_application_emoji`, disponível em discord.py >= 2.5. Os nomes são
determinísticos, então a mesma imagem é reutilizada em todos os servidores e
após reiniciar o bot. Nenhum emoji é criado por usuário/servidor.

A sincronização ocorre em segundo plano e não bloqueia a abertura do menu.
Se o Discord rejeitar a criação ou a imagem não estiver instalada, o seletor
continua funcionando apenas com o nome da música. A criação não é repetida
a cada clique: tentativas são limitadas e emojis já existentes são reaproveitados.
O painel principal mantém a imagem em tamanho maior; no seletor o tamanho da
miniatura é limitado pelo tamanho do emoji nativo do Discord.

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

Use o kit Termux `import-midi.py` com MIDIs **locais**, nunca anexos Discord.
Ele reutiliza o parser de partituras de `cogs/tts/score.py` e uma cópia
sincronizada da normalização de metadados de `cogs/musica/metadados/normalizacao.py`.
Procura até 25 candidatos no YouTube e só aceita contagens verificáveis dos
vídeos correspondentes; a maior contagem *entre os vídeos inspecionados* é
selecionada. Vídeos distintos, covers e remixes são rejeitados sem adivinhar.

O Termux gera ZIPs com os catálogos completos e a nova capa, que devem ser
aplicados pelo updater da VPS. A publicação automática remota, com autenticação,
ainda não foi implementada. A atualização do catálogo e dos painéis **já abertos**
é detectada até 15 s após a aplicação (com bot em execução). O menu tem
paginação a cada 25 opções, respeitando os limites do Discord.

O emoji é versionado pelo SHA-256 da capa e reaproveitado entre servidores.
A borda do painel acompanha a cor predominante local da capa (ou do avatar do
bot no Padrão); a extração mantém imagens em preto e branco sem matiz falsa.
O cache de cores é limitado a 96 números RGB, sem guardar os JPEGs em RAM.

O Termux prepara uma amostra UTAU em `pt-BR` para as preferências padrão quando
as condições de bateria permitem. O bot verifica o SHA da amostra, a versão
da partitura e as preferências antes de reutilizar o MP3. Em outras
configurações, usa a síntese de amostras normal do Phone Worker.
Se a geração estiver bloqueada, o kit conserva a pendência para nova tentativa
quando for executado novamente. O preview pronto pode ser reproduzido com o
worker temporariamente offline.
