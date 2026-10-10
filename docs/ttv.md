# TTV (TextToVocaloid)

Aplicar este patch pelo updater e abrir um painel novo com `_tts`. A seção
TTV mantém o prefixo configurado anteriormente para a Teto e permite escolher
personagem, tom e velocidade. No catálogo atual aparece somente Kasane Teto.

O modal **Configurar TTV** usa Labels sem descrições, com um String Select e
dois RadioGroups de cinco opções, na mesma ordem do Edge:

| Campo | Opções |
| --- | --- |
| Escolha sua vocaloid | Kasane Teto |
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
em canal de voz. Restaurar conserva a personagem e volta ao tom 0 e à taxa 1.
O bloco TTV aparece enquanto o worker está online. O resumo exibe a voz e
somente tom e velocidade diferentes do padrão, como os outros modos. O tom
usa a unidade compacta `st` (semitons). Clientes que
recusam os componentes novos recebem três campos de texto com os valores
atuais preenchidos.

## Compatibilidade e extensão

`cogs/tts/ttv.py` centraliza o catálogo. Os IDs das personagens são estáveis;
`kasane-teto` é distinto dos perfis Standard/English da voicebank. Adicionar
uma personagem exige também um renderer instalado e suporte no worker; uma
voz anunciada na interface não representa, sozinha, capacidade de síntese.

O banco guarda `ttv_voice_id`, `ttv_pitch_semitones` e `ttv_speech_rate`
separadamente dos controles Edge/gTTS. O tom legado `teto_pitch_semitones`
continua sendo lido e espelhado quando editado. Valores antigos como -2,0
continuam selecionados até uma edição ou restauração explícita.

O identificador interno `teto`, os comandos existentes e os prefixos são
preservados. Os controles percorrem mensagem, fila, payload, worker e renderer.
Os caches incluem personagem, tom, taxa e fingerprint da implementação/bank.
O áudio de TTV não muda silenciosamente para outra personagem em caso de erro.

## Atualização e verificação

O patch mantém as pastas originais do repositório e não inclui voicebanks,
binários dos motores, segredos ou um manifesto de exclusões. A automação
existente do updater publica o worker **1.11.34**, que aceita a nova velocidade
de 50%. O bootstrap no telefone
promove a release e reinicia o processo; o ciclo normal de consulta é de cinco
minutos, podendo ter alguns segundos de variação. Não há necessidade de
reinstalar ESPER para esta mudança.

Os testes cobrem serialização real dos componentes em discord.py 2.7.1,
controle de acesso, respostas privadas, restauração, migração dos ajustes,
payloads, metadados raw, separação dos caches e velocidade nos dois renderers.
A execução local dos testes não confirma a implantação no Discord ou no Poco.
A inteligibilidade e o timbre em português devem ser avaliados ouvindo as
amostras; gerar um WAV válido não mede a naturalidade da fala.
