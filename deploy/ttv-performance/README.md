# Canto MIDI e efeitos de TTV

Este pacote usa a base `repo-20261011-161817.zip` e acrescenta um fluxo de
execução musical ao worker 1.11.40. A fala sem comandos mantém seus controles
e seu renderer atual. O canto utiliza o renderizador UTAU do Phone Worker com o resampler configurado
e o banco English-CVVC instalado para a voz selecionada.

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

## Cantar pelo catálogo `_cd`

No Discord, escolha a música no painel `_cd` e use `/cantar` junto ao texto
TTV. O bot usa a partitura cadastrada e os ajustes da voz selecionada.
Exemplo com prefixo `'`: `'/cantar a ~ ~ mor`.

O bot ignora anexos `.mid`/`.midi` enviados pelos usuários para fins de canto:
não lê o arquivo, não oferece seleção de faixas e não altera a melodia do
catálogo. MIDI permanece disponível **somente** nas ferramentas locais de
análise e teste descritas abaixo.

O sintetizador mantém o backend UTAU configurado no Phone Worker; canto usa a
voicebank English-CVVC, inclusive para letras em português. O resampler não é
trocado automaticamente. Os limites de duração e notas vêm do contrato atual
de partitura, que é compartilhado por catálogo e ferramentas offline.

## Atualização do Phone Worker

O ZIP do updater contém somente os arquivos da VPS. As correções do renderizador
e do processo Android são distribuídas no **kit Termux separado**, que deve ser
instalado no telefone. Atualizar o bot na VPS não atualiza automaticamente
este kit.

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

Aplique o ZIP da VPS somente pelo updater do bot. O kit Termux atualiza
separadamente os arquivos de síntese do Phone Worker no Android; seu instalador
confere hashes e cria backup antes de alterar a release ativa. As voicebanks,
resampler e configuração de voz locais não são modificados pelo kit.

A síntese real precisa ser validada no telefone; testes automatizados com
resampler de teste não demonstram qualidade de voz da instalação ARM64.
