# TextToTeto com VOICEPEAK

Esta integração mantém a engine `teto` e o prefixo `'` do bot. O Poco/Termux
envia o texto para um host com VOICEPEAK e recebe WAV. A leitura em português
é experimental: o adaptador converte os sons para kana e o motor japonês os
pronuncia. Não traduz a mensagem para japonês.

## Requisitos e estado

Instale e ative legalmente o VOICEPEAK da Kasane Teto em um sistema compatível.
A [página oficial](https://www.ah-soft.com/voice/teto/) informa Windows,
macOS e Ubuntu 64 bits, com mínimo de **2 GB de RAM**. Confirme também a CPU e
a versão do sistema na página. Não há versão nativa para Android/Termux.
Uma VPS de 1 GB fica abaixo do mínimo: não há garantia de inicialização,
estabilidade ou latência. Os 11 GB do telefone não aumentam a RAM da VPS.

O ZIP fornece somente a integração. Não inclui o programa comercial, a voz,
ativação ou credenciais. Os testes automatizados usam um CLI simulado;
nenhuma síntese com o VOICEPEAK licenciado foi validada nesta entrega.

## Configurar o host de síntese

Disponibilize neste host o repositório com os arquivos deste patch. O bridge
usa Python 3.10+ e biblioteca padrão, além do VOICEPEAK instalado. Não precisa
instalar os pacotes do bot Discord para executar o bridge.

1. Copie `deploy/voicepeak-teto/voicepeak.env.example` para `voicepeak.env`.
2. Preencha `PHONE_WORKER_VOICEPEAK_COMMAND` com o caminho real do executável.
   É um único caminho, sem argumentos. Se houver espaços, use aspas ao definir
   a variável pelo shell.
3. Confira `voicepeak --list-narrator` usando o executável instalado. Configure
   o nome exato da Teto retornado: `重音テト`, `Kasane Teto` ou `Teto`.
4. Troque `VOICEPEAK_TOKEN` por um token aleatório. Por exemplo, gere um com
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
5. Para acesso pelo telefone, configure `VOICEPEAK_HOST` com o IP privado do
   host acessível pelo Termux, por exemplo na sua rede Tailscale. O padrão
   `127.0.0.1` atende somente clientes locais. Use HTTP em rede privada ou
   HTTPS por um proxy quando necessário.

Em Linux/macOS, na raiz do repositório:

```bash
set -a
source ./voicepeak.env
set +a
python3 deploy/voicepeak-teto/validate.py --render-test --output teto-voicepeak-teste.wav
python3 deploy/voicepeak-teto/server.py
```

Ouça o WAV antes de ativar no bot: `ready=true` sozinho verifica o executável e
o inventário, mas não comprova uma síntese bem-sucedida. No Windows, defina as
mesmas variáveis de ambiente no PowerShell e execute os scripts com Python;
o arquivo de exemplo usa sintaxe de shell Linux e não é um script PowerShell.

Velocidade, tom e expressões são controlados **no host**:

```dotenv
PHONE_WORKER_VOICEPEAK_SPEED=100
PHONE_WORKER_VOICEPEAK_PITCH=0
PHONE_WORKER_VOICEPEAK_EMOTION=
```

Os limites são velocidade `50..200`, tom nativo `-300..300` e intensidade de
expressão `0..100`. Consulte `--list-emotion` com o narrador instalado antes de
configurar `nome=valor`. Não suponha que os nomes da interface sejam os nomes
aceitos pelo CLI. O controle de semitons UTAU não altera este motor.

## Configurar o phone worker

No `~/.phone-worker.env` existente, ajuste estas chaves, preservando o restante
do pareamento. Use o IP privado real do host e o mesmo token:

```dotenv
PHONE_WORKER_TETO_ENABLED=true
PHONE_WORKER_TETO_BACKEND=voicepeak
PHONE_WORKER_VOICEPEAK_URL=http://IP_PRIVADO_DO_HOST:8087
PHONE_WORKER_VOICEPEAK_TOKEN=MESMO_TOKEN_DO_HOST
PHONE_WORKER_VOICEPEAK_NARRATOR=重音テト
PHONE_WORKER_VOICEPEAK_TEXT_MODE=ptbr-kana
PHONE_WORKER_VOICEPEAK_ALLOW_OTHER_VOICES=false
```

O narrador e o modo de leitura devem coincidir com os do host. Reinicie pelo
supervisor instalado:

```bash
bash ~/.core-worker-runtime/current/start-phone-worker.sh --force-restart
```

O status `/tts-agent/status` passa a informar `teto.backend=voicepeak`,
`reading_mode=ptbr-kana` e `ready`. A versão do worker é `1.11.28`. O texto
`'Olá, eu sou a Teto` segue usando o prefixo já configurado no bot.
Se o host falhar, a solicitação explícita à Teto retorna erro; não troca
silenciosamente a personagem por outra voz. Para retornar à síntese UTAU,
configure `PHONE_WORKER_TETO_BACKEND=utau` e reinicie.

O updater preserva a configuração existente: aplicar o ZIP não ativa o
VOICEPEAK sozinho. O backend padrão continua `utau` até configurar o host.

## Limites do português experimental

O VOICEPEAK japonês não ganha suporte nativo a português com este adaptador.
Encontros consonantais, nasalização, `r`, `lh`, `nh` e acentuação podem continuar
soando diferentes; a inteligibilidade exige teste auditivo. A adaptação mantém
pontuação e espaços. Números são lidos dígito a dígito. Entradas que o adaptador
não consegue pronunciar retornam erro, sem desaparecer da frase.

O CLI recebe trechos de até 140 caracteres **após** a conversão. O bridge junta
os WAVs preservando o formato PCM. Os padrões são até 180 caracteres de entrada,
20 segundos de áudio e 8 MiB; status e renderização compartilham o prazo da
requisição. O host serializa a síntese e responde ocupado quando necessário.

Referências: [produto Teto](https://www.ah-soft.com/voice/teto/),
[notas do VOICEPEAK com CLI](https://www.ah-soft.com/voice/setup/),
[uso de leitura kana](https://www.ah-soft.com/voice/manual/03_useage.html#3-12).
