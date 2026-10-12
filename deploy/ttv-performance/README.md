# Canto MIDI e efeitos de TTV

Este pacote usa a base `repo-20261011-161817.zip` e acrescenta um fluxo de
execução musical ao worker 1.11.40. A fala sem comandos mantém seus controles
e seu renderer atual. O canto usa ESPER e os bancos English-CVVC já instalados
da Miku e da Teto.

## Fala com efeitos

Use o prefixo de TTV configurado no seu servidor. Os exemplos usam `'`:

| Mensagem | Resultado |
| --- | --- |
| `'<aaa aaa>` | Entrada suave no primeiro “aaa”, saída suave no segundo |
| `'Olá \| tudo bem?` | Pausa curta entre os trechos |
| `'aaa~` | Sustenta a última vogal |

`<` fica imediatamente antes da palavra; `>` e `~` ficam no final. A duração
dos fades é calculada a partir da duração da palavra. Para símbolos literais,
escape-os com uma barra invertida, como `\<`; menções, links e código não são
interpretados como efeitos. Os efeitos valem somente no TTV. Mensagens com
efeitos têm limite de 180 caracteres e são renderizadas como uma apresentação
única, sem divisão em blocos que perderia as transições.

## Cantar um MIDI

Anexe um arquivo `.mid` ou `.midi` à mensagem de TTV e escreva a letra depois
do prefixo. Exemplo: `'abacate com manteiga`. A voz e o idioma continuam sendo
os selecionados em **Configurar TTV**. O canto preserva o andamento e as notas
do arquivo, separado dos ajustes de tom e velocidade da fala.

A letra da mensagem tem prioridade. Sem texto, o bot usa a letra temporizada
do MIDI; sem letra no arquivo, canta a vogal “a”. O leitor prefere uma melodia
monofônica com letra ou uma faixa identificada como vocal/melody/lead. Faixas
de bateria não são usadas. Quando há várias candidatas, escolha a melodia no
menu oferecido pelo bot.

As sílabas são alinhadas às notas; notas conectadas podem prolongar uma vogal.
Quando a letra não cabe, a resposta informa o problema para ajustar o trecho.
O exemplo `a ~ ~ mor` mantém o “a” durante três notas e começa “mor” na quarta.
O `|` separa frases no canto e conserva o andamento e as pausas do MIDI.
Entrada e saída suaves também podem ser aplicadas às palavras da letra.

O limite inicial é uma melodia, 128 notas, 60 segundos e arquivo de 512 KiB.
Uma vogal contínua pode ocupar até 18 segundos nesta versão; para sustentações
maiores, divida a letra em outro ataque ou acrescente uma pausa no MIDI. Esse
limite mantém cada fragmento dentro do orçamento aceito pela ponte ESPER
instalada no Termux. A duração total do canto continua podendo chegar a 60 segundos.
São aceitos MIDI tipo 0/1 com divisão PPQ e mudanças de andamento. Acordes na
faixa escolhida, MIDI tipo 2 e divisão SMPTE precisam ser exportados como uma
melodia compatível antes de enviar. O arquivo fornece a melodia da voz; o bot
não toca automaticamente um acompanhamento instrumental.

## Teste independente no Termux

Extraia o kit em uma pasta própria e execute:

```bash
python "$HOME/ttv-canto-termux-kit/deploy/ttv-performance/termux/render-demo.py" \
  --container voicepeak-arm64 --esper-root "$HOME/.esper-utau"
```

Sem MIDI, o script cria uma melodia curta para as duas vozes. Os WAVs, MP3s,
MIDI e relatório ficam em uma pasta nova dentro de `Download/ttv-canto`; o
resultado mostra o caminho completo. Para uma melodia sua:

```bash
python "$HOME/ttv-canto-termux-kit/deploy/ttv-performance/termux/render-demo.py" \
  --container voicepeak-arm64 --esper-root "$HOME/.esper-utau" \
  --midi "$HOME/storage/downloads/melodia.mid" --text "a ~ ~ mor" --voice miku
```

Para testar os efeitos de fala, use `--speech --text '<aaa aaa>'`. Para inglês,
adicione `--language en-US`. Use `--track N` para escolher explicitamente uma
faixa quando o MIDI tiver várias candidatas. O script preserva a configuração,
os bancos, a instalação do ESPER e os caches de produção; usa caches temporários.

## Atualização

Aplique o patch pelo updater do bot e confirme **1.11.40** no status do worker
antes de testar. Um worker anterior recebe pedidos de fala comum; pedidos de
canto e efeitos exigem suporte específico e são recusados com uma mensagem de
atualização. Nenhum banco de voz ou binário do ESPER está incluído no patch.

O teste local com ESPER Linux x64 confirma execução e características medidas
do áudio. O teste do kit no telefone valida a instalação ARM64 existente.
Pronúncia percebida e sonoridade devem ser avaliadas ouvindo as amostras.
