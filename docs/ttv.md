# TTV (TextToVocaloid)

Aplicar este patch pelo updater e abrir um painel novo com `_tts`. A seção
TTV mantém o prefixo configurado anteriormente para a Teto e permite escolher
personagem, tom e velocidade. No catálogo atual aparece somente Kasane Teto.

O modal **Configurar TTV** usa Labels com dois String Selects e um RadioGroup:

| Campo | Opções |
| --- | --- |
| Escolha sua vocaloid | Kasane Teto |
| Tom da voz | Original ou -4 a +4 semitons, em passos de 0,5 |
| Velocidade da fala | 85%, 100%, 115%; valor personalizado atual, se houver |

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
existente do updater publica o worker **1.11.33**. O bootstrap no telefone
promove a release e reinicia o processo; o ciclo normal de consulta é de cinco
minutos, podendo ter alguns segundos de variação. Não há necessidade de
reinstalar ESPER para esta mudança.

Os testes cobrem serialização real dos componentes em discord.py 2.7.1,
controle de acesso, respostas privadas, restauração, migração dos ajustes,
payloads, metadados raw, separação dos caches e velocidade nos dois renderers.
A execução local dos testes não confirma a implantação no Discord ou no Poco.
A inteligibilidade e o timbre em português devem ser avaliados ouvindo as
amostras; gerar um WAV válido não mede a naturalidade da fala.
