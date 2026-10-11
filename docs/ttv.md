# TTV (TextToVocaloid)

Aplicar este patch pelo updater e abrir um painel novo com `_tts`. A seção
TTV mantém o prefixo configurado anteriormente para a Teto e permite escolher
personagem, idioma, tom e velocidade. Kasane Teto continua sendo o padrão.
Hatsune Miku aparece quando sua voicebank English-CVVC está instalada e pronta
no worker, sem substituir a escolha dos usuários existentes.

O modal **Configurar TTV** usa Labels sem descrições, com um String Select e
um RadioGroup de idioma e dois RadioGroups de cinco opções, na mesma ordem do Edge:

| Campo | Opções |
| --- | --- |
| Escolha sua vocaloid | Kasane Teto, Hatsune Miku (quando instalada e pronta) |
| Idioma | Português adaptado (padrão), Inglês |
| Velocidade da fala | Bem mais lenta, Mais lenta, Normal, Mais rápida, Bem mais rápida |
| Tom da voz | Bem mais grave, Mais grave, Normal, Mais agudo, Bem mais agudo |

Velocidades: 0,5 / 0,75 / 1 / 1,25 / 1,5, equivalentes aos ajustes de -50%,
-25%, 0%, +25% e +50% do Edge. Tons: -4 / -2 / 0 / +2 / +4 semitons; Hz do
Edge não é a unidade do renderer Teto. Os valores de meios semitons e
velocidades personalizadas antigos continuam válidos. Se o valor salvo não
for um dos cinco presets, o título mostra o valor atual e o grupo fica sem
seleção obrigatória; enviar sem selecionar conserva esse valor, sem acrescentar
uma sexta opção nem selecionar um preset diferente.

Depois de salvar, a confirmação privada oferece **Ouvir amostra** e
**Restaurar padrões**. A amostra usa os ajustes pessoais em um MP3 sem entrar
em canal de voz. Restaurar conserva a personagem e o idioma e volta ao tom 0 e à taxa 1.
O bloco TTV aparece enquanto o worker está online. O resumo exibe a voz e
somente tom e velocidade diferentes do padrão, como os outros modos. O tom
usa a unidade compacta `st` (semitons). Clientes que
recusam os componentes novos recebem quatro campos de texto com os valores
atuais preenchidos.

## Idioma de leitura

**Português adaptado** conserva a pronúncia PT-BR existente. **Inglês** lê o
texto em inglês, com pronúncias e acentos do CMUdict e aproximações para palavras
fora do dicionário. A seleção não traduz a mensagem. A amostra usa `Olá. Como
você está?` em português e `Hello. How are you?` em inglês.

Para a Teto, o inglês exige a voicebank English-CVVC da própria Teto, instalada em
`~/voicebanks/kasane-teto-english` ou no caminho
`PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR`. A escolha é feita por pedido e não
muda o perfil global nem os ajustes de outros usuários. Sem essa bank ou sem
suporte no worker, a leitura em inglês é recusada. O dicionário funciona offline,
vem embutido no módulo existente e preserva a licença BSD e a identificação da
fonte. Não requer outro serviço, Box64 ou reinstalação do ESPER.

Hatsune Miku usa uma voicebank English-CVVC separada, configurada em
`PHONE_WORKER_MIKU_VOICEBANK_DIR` (padrão: `~/voicebanks/hatsune-miku-english`).
Ela oferece os mesmos dois idiomas de leitura, com ESPER. Cada pedido usa o
banco da personagem escolhida; o idioma não seleciona outra personagem.
Instalar esse banco não altera a Teto nem a seleção pessoal. Um banco ausente
deixa a escolha salva identificada como indisponível e impede mensagens/amostras
com essa voz. Não há substituição automática pela Teto.

## Compatibilidade e extensão

`cogs/tts/ttv.py` centraliza o catálogo. Os IDs das personagens são estáveis;
`kasane-teto` e `hatsune-miku` são distintos dos perfis Standard/English da voicebank. Adicionar
uma personagem exige também um renderer instalado e suporte no worker; uma
voz anunciada na interface não representa, sozinha, capacidade de síntese.

O banco guarda `ttv_voice_id`, `ttv_language`, `ttv_pitch_semitones` e `ttv_speech_rate`
separadamente dos controles Edge/gTTS. O tom legado `teto_pitch_semitones`
continua sendo lido e espelhado quando editado. Valores antigos como -2,0
continuam selecionados até uma edição ou restauração explícita.

O identificador interno `teto`, os comandos existentes e os prefixos são
preservados. Os controles percorrem mensagem, fila, payload, worker e renderer.
Os caches incluem personagem, idioma, tom, taxa e fingerprint da implementação/bank
anunciada para essa personagem. As impressões digitais e os idiomas legados da
Teto só podem ser usados como compatibilidade para a própria Teto.
O áudio de TTV não muda silenciosamente para outra personagem em caso de erro.

## Atualização e verificação

O patch mantém as pastas originais do repositório e não inclui voicebanks,
binários dos motores, segredos ou um manifesto de exclusões. A automação
existente do updater publica o worker **1.11.37**, que anuncia as vozes prontas
separadamente e sintetiza a personagem selecionada por pedido. Ele conserva a
correção do tom **Normal** nas respostas raw da versão 1.11.36.
O bootstrap no telefone
promove a release e reinicia o processo; o ciclo normal de consulta é de cinco
minutos, podendo ter alguns segundos de variação. Não há necessidade de
reinstalar ESPER para esta mudança.

Na versão 1.11.35, o worker gerava áudio com tom zero, mas enviava esse valor
como um cabeçalho vazio. O bot recusava a resposta com a mensagem de ajustes
diferentes dos solicitados. A versão 1.11.36 preserva o zero na serialização;
a validação dos ajustes pessoais continua exigindo os valores efetivos.

Os testes cobrem serialização real dos componentes em discord.py 2.7.1,
controle de acesso, respostas privadas, restauração, migração dos ajustes,
payloads, metadados raw, separação dos caches, idioma e velocidade nos dois renderers.
Os testes de integração também usam um servidor HTTP local do worker e o cliente
real do bot, cobrindo tom zero nos dois idiomas, cache e conversão da amostra em MP3.
A execução local dos testes não confirma a implantação no Discord ou no Poco.
A inteligibilidade e o timbre em português devem ser avaliados ouvindo as
amostras; gerar um WAV válido não mede a naturalidade da fala.
