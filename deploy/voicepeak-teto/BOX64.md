# VOICEPEAK no Termux com Ubuntu ARM64 + Box64

Esta é a alternativa ao Ubuntu x86_64/QEMU que apresentou SIGSEGV no
`ldconfig.real` do Poco. O Ubuntu 24.04 **ARM64**, seu `apt`, `dpkg` e as
bibliotecas do sistema rodam na arquitetura do telefone. O Box64 emula
somente o programa Linux x86_64 do VOICEPEAK.

Não precisa de root. No Poco X7 Pro, o Ubuntu ARM64, Box64 v0.4.0 e o
`--help` do VOICEPEAK já foram confirmados. A primeira tentativa de GUI
terminou com sinal 11; interface, ativação e síntese ainda precisam ser
verificadas. O kit não contém o motor
comercial, a voz Teto ou uma licença.

## Instalar o kit e o runtime

Baixe `teto-voicepeak-termux-box64-kit-v5.zip` para Downloads. No Termux nativo,
fora de outro PRoot:

```bash
unzip -o "$HOME/storage/downloads/teto-voicepeak-termux-box64-kit-v5.zip" -d "$HOME/voicepeak-termux-kit"
cd "$HOME/voicepeak-termux-kit"
bash deploy/voicepeak-teto/termux/setup-box64.sh
```

O setup cria `voicepeak-arm64` separado do container anterior `voicepeak-x64`.
Instala pacotes ARM64 e compila Box64 com dois processos de compilação. Essa
etapa pode demorar vários minutos; reserve alguns GB de armazenamento livre.
Uma falha preserva o container para investigação e impede anunciar o runtime
como pronto.

O kit v2 instala também `python3` dentro do Ubuntu e seleciona
`/usr/bin/python3` no CMake. Se a primeira tentativa parou em
`Could NOT find Python3`, execute novamente o setup atualizado: ele reutiliza
o container, o checkout e a pasta de compilação existentes.

O kit v3 encaminha opções de diagnóstico do Box64 explicitamente ao Ubuntu,
pois o PRoot limpa as variáveis do Termux. Também inclui `x11-utils` para
testar a conexão gráfica com clientes ARM64 e `libxss1`, usado opcionalmente
pela interface do VOICEPEAK. Essa biblioteca adicional não é uma correção
confirmada para o sinal 11. Reexecutar o setup mantém o checkout, o programa
instalado e a compilação existente.

O kit v4 inclui um teste Xlib x86_64 independente do VOICEPEAK e opções para
registrar as últimas chamadas e iniciar as threads X11 antecipadamente.
O `xmessage` ARM64 já abriu no Poco. A interface do VOICEPEAK caiu também
sem Dynarec, em `XGetWindowProperty` da biblioteca X11 nativa; desativar o
Dynarec não resolveu essa tentativa. O mesmo programa abriu em Linux x86_64
nativo, sem gerenciador de janelas. O probe v4 passou nas duas configurações
de threads no Poco, incluindo propriedades e callback de erro.

O trace do programa real mostrou a chamada a `XGetWindowProperty` com
display, janela e átomo nulos. O JUCE 7.0.12 usado pelo VOICEPEAK exige 111
símbolos Xlib e interrompe a inicialização se algum estiver ausente. Box64
v0.4.0 não exporta `XPutPixel`, o único obrigatório ausente nessa comparação.
O kit v5 implementa esse wrapper numa cópia isolada do código do Box64,
restaurando os callbacks da imagem após a chamada nativa. O programa
comercial não é modificado. O funcionamento da GUI corrigida ainda precisa
ser confirmado no Poco.

O código do Box64 fica fixado na versão oficial **v0.4.0**, commit
`dae0917c47b4edd8956f314210417a20fd225c4b`. O setup também usa as duas
bibliotecas abertas x86_64 incluídas nesse commit, `libstdc++.so.6` e
`libgcc_s.so.1`, sem instalar pacotes amd64 no Ubuntu ARM64. Ubuntu 24.04 foi
escolhido porque a glibc 2.39 atende à exigência GLIBC_2.36 de uma dessas
bibliotecas. Nenhuma biblioteca comercial é distribuída no kit.

O executável nativo fica em `/opt/voicepeak-box64/bin/box64`; as duas
bibliotecas x64 ficam em `/opt/voicepeak-box64/lib/x86_64-linux-gnu`.
O checkout existente é preservado se tiver um commit diferente ou alterações
locais. O setup não registra binfmt nem substitui executáveis do Ubuntu.

## Corrigir XPutPixel num runtime já instalado

Para o Poco que já completou o setup, extraia o kit v5 e execute:

```bash
unzip -o "$HOME/storage/downloads/teto-voicepeak-termux-box64-kit-v5.zip" -d "$HOME/voicepeak-termux-kit"
cd "$HOME/voicepeak-termux-kit"
bash deploy/voicepeak-teto/termux/patch-box64-x11.sh
```

Esse instalador usa o container existente. Valida o checkout limpo no commit
fixado e os hashes dos dois arquivos que precisam da correção. Cria
`/opt/voicepeak-box64/src-x11fix-1` e compila em
`/opt/voicepeak-box64/build-x11fix-1`, sem modificar a fonte ou o binário
originais. A primeira compilação pode demorar vários minutos.

O binário separado é `/opt/voicepeak-box64/bin/box64-x11fix-1`. Antes de
publicar os launchers, o instalador exige o sucesso do probe dos 111 símbolos,
que dispensa servidor X11. A configuração separada é
`~/.voicepeak-termux/config-box64-x11fix.json`; os aliases são
`voicepeak-termux-box64-x11fix` e
`voicepeak-termux-box64-x11fix-diagnostic`. A pasta do programa, o display e
o container vêm da configuração anterior. Configurações e aliases externos
ao instalador são preservados.

A cópia corrigida tem um inventário de arquivos para detectar alterações
locais antes de uma tentativa posterior. Somente arquivos explicitamente
gerados pelo build são excluídos dessa comparação. Uma falha não anuncia a
correção como instalada.

Com o Termux:X11 iniciado, teste símbolos, propriedades e pixels pelo
runtime corrigido:

```bash
VOICEPEAK_TERMUX_CONFIG="$HOME/.voicepeak-termux/config-box64-x11fix.json" \
DISPLAY=:1 python deploy/voicepeak-teto/termux/probe-x11.py
```

O JSON `voicepeak-box64-x11-probe.json` é salvo em Downloads quando essa
pasta está disponível. Deve mostrar os dois checks com `ok: true`. Agora o
probe verifica todos os símbolos obrigatórios e exercita `XPutPixel` numa
imagem real, incluindo chamadas dos callbacks após sua restauração.

Se passar, teste a interface com a mesma emulação usada pelo probe:

```bash
DISPLAY=:1 BOX64_DYNAREC=0 BOX64_LOG=1 BOX64_NOBANNER=0 BOX64_SHOWSEGV=1 BOX64_SHOWBT=0 BOX64_ROLLING_LOG=64 \
  timeout -k 5s 180s "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64-x11fix" \
  > "$HOME/storage/downloads/voicepeak-box64-x11fix-gui.log" 2>&1
```

Abra o aplicativo Termux:X11 para conferir a janela. Se houver outra queda,
envie o JSON e o log completo. Sucesso do probe comprova esse caminho Xlib;
GUI, ativação, síntese e latência são verificações separadas. Depois de a GUI
abrir, teste novamente com `BOX64_DYNAREC=1` para avaliar o modo mais rápido.
O launcher anterior continua disponível como comparação.

## Testar o programa oficial antes de comprar a voz

Se o programa ainda não foi instalado, na raiz do kit:

```bash
python deploy/voicepeak-teto/termux/fetch-engine.py
"$HOME/.voicepeak-termux/bin/voicepeak-termux-box64-diagnostic" --probe-system --probe-runtime --timeout 120
```

O downloader baixa o pacote oficial público 1.2.22, aproximadamente 194 MiB,
e verifica o SHA-256 do arquivo Linux. Essa versão é fixa para o primeiro
teste de compatibilidade; não inclui a voz Teto. Se o programa já existir, o
downloader recusa sobrescrevê-lo. Nesse caso, execute apenas o diagnóstico.

O launcher usa `~/.voicepeak-termux/config-box64.json` e apresenta a pasta
persistente do programa como `/opt/Voicepeak`. O alias
`voicepeak-termux-box64` seleciona essa configuração automaticamente;
`voicepeak-termux` e a configuração QEMU antiga continuam separados.

O diagnóstico deve mostrar:

- `system_probe.guest_architecture = "arm64"`;
- `system_probe.system_healthy = true`;
- `system_probe.box64_verified = true`;
- `engine_elf_x86_64 = true`;
- `cli_help_ok = true` quando o programa realmente retornar suas opções.

Sem a voz instalada e ativada, `teto_inventory_ok = false` e
`engine_verified = false` são esperados. O inventário pode expirar enquanto o
programa espera a ativação. A abertura do `--help` comprova somente essa etapa,
sem comprovar GUI ou síntese.

Para testar apenas a abertura do programa:

```bash
timeout 90 "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64" --help
```

Envie a saída se houver erro de biblioteca, crash ou ausência das opções do
programa. O diagnóstico identifica mensagens de término por sinal mesmo
quando PRoot devolve código zero. Os comandos de bibliotecas verificam o
`ldconfig.real` diretamente, evitando usar o wrapper como prova de execução.

## Interface gráfica e ativação

Depois que o programa abrir, instale o APK e o pacote complementar do
[Termux:X11](https://github.com/termux/termux-x11) conforme o projeto oficial.
Instale `termux-x11-universal-debug.apk` do release nightly no Android e,
no Termux:

```bash
pkg install -y x11-repo && pkg install -y termux-x11-nightly
termux-x11 :1 &
DISPLAY=:1 "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64"
```

Abra o aplicativo Termux:X11 no Android. A voz Teto exige compra, instalação
e ativação oficiais pela interface do VOICEPEAK. Esse passo não foi validado
no Poco. A emulação não fornece licença nem acrescenta suporte nativo a
português.

### Investigar sinal 11 ao abrir a interface

A CLI `--help` não exercita a interface gráfica. Tela preta com cursor pode
ser o servidor X11 sem janelas após o encerramento do programa. O aviso sobre
`/sys/module/mali_kbase/parameters/large_page_conf` não identifica por si só
a causa do crash. Mantenha o servidor `termux-x11 :1` iniciado.

#### Testar a ponte Xlib x86_64

Na raiz do kit v5, escolha a configuração corrigida:

```bash
VOICEPEAK_TERMUX_CONFIG="$HOME/.voicepeak-termux/config-box64-x11fix.json" \
DISPLAY=:1 python deploy/voicepeak-teto/termux/probe-x11.py
```

Esse probe usa os arquivos do próprio kit e a configuração existente;
depois de instalar a correção, não exige outra compilação. A configuração
original é útil como controle negativo: deve falhar ao buscar `XPutPixel`,
antes de abrir o display, em vez de anunciar sucesso parcial.

O teste exige os 111 símbolos Xlib do JUCE, testa pixels e verifica `XGetWindowProperty` para
propriedades válidas de 8 e 32 bits, propriedade ausente e callback de erro
para um átomo inválido. Usa uma janela própria invisível e a destrói depois;
não executa o VOICEPEAK. Repete pelo interpretador, com e sem
`BOX64_X11THREADS=1`, que chama `XInitThreads` logo ao carregar Xlib. Isso é
uma comparação, não uma correção comprovada para o crash.

O JSON preserva o estágio e a saída quando há falha. Sucesso requer tanto o
marcador final `VOICEPEAK_X11_PROBE_OK` quanto código zero, sem mensagem de
término por sinal. PRoot pode retornar zero mesmo quando o guest cai. Envie
`voicepeak-box64-x11-probe.json`, salvo em Downloads quando a pasta está
disponível, ou na pasta pessoal do Termux.

O kit contém o código-fonte aberto `x11-probe.c` e seu pequeno binário x86_64,
compilado com `gcc -O2 -Wall -Wextra ... -ldl`, exigindo no máximo GLIBC 2.34.
Ele passou nativamente num Xvfb sem gerenciador de janelas. Nenhum binário
comercial acompanha o arquivo.

#### Registrar as chamadas antes do crash

Com o launcher v4, registre o VOICEPEAK pelo interpretador sem a saída de
backtrace que gerou os avisos repetidos `LSDA unsupported`:

```bash
DISPLAY=:1 BOX64_LOG=1 BOX64_NOBANNER=0 BOX64_DYNAREC=0 BOX64_SHOWSEGV=1 BOX64_SHOWBT=0 BOX64_ROLLING_LOG=64 \
  timeout -k 5s 180s "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64" 2>&1 | tee "$HOME/voicepeak-box64-gui-calls.log"
```

`BOX64_ROLLING_LOG` registra as últimas chamadas nativas antes do sinal;
Box64 o desativa se `BOX64_LOG` for maior que 1. O aviso LSDA é relacionado
ao backtrace e não demonstra a causa inicial da queda. Se for necessário
comparar inicialização antecipada de threads no programa, acrescente
`BOX64_X11THREADS=1` ao mesmo comando e salve em outro log.

#### Conferir a conexão ARM64 e comparar Dynarec

Após instalar o kit v3 e reexecutar o setup, teste uma janela ARM64 sem Box64:

```bash
proot-distro login voicepeak-arm64 --shared-tmp -- /usr/bin/env DISPLAY=:1 /usr/bin/xdpyinfo > "$HOME/voicepeak-x11-probe.log" 2>&1 &&
proot-distro login voicepeak-arm64 --shared-tmp -- /usr/bin/env DISPLAY=:1 /usr/bin/xmessage -center -timeout 30 "Ubuntu ARM64 conectado ao Termux:X11"
```

Abra o aplicativo Termux:X11 para conferir a mensagem. Se `xdpyinfo` falhar,
envie `voicepeak-x11-probe.log` antes de testar a emulação. Se a conexão for
confirmada, registre a execução com Dynarec:

```bash
DISPLAY=:1 BOX64_LOG=2 BOX64_NOBANNER=0 BOX64_DYNAREC=1 BOX64_SHOWSEGV=1 BOX64_SHOWBT=1 \
  "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64" 2>&1 | tee "$HOME/voicepeak-box64-gui-dynarec.log"
```

Se o programa cair, compare sem Dynarec, usando o interpretador:

```bash
DISPLAY=:1 BOX64_LOG=2 BOX64_NOBANNER=0 BOX64_DYNAREC=0 BOX64_SHOWSEGV=1 BOX64_SHOWBT=1 \
  timeout -k 5s 180s "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64" 2>&1 | tee "$HOME/voicepeak-box64-gui-interpreter.log"
```

O interpretador pode ser bem mais lento; o teste termina após três minutos
ou ao fechar o programa. Envie os dois logs e informe se alguma janela abriu.
Eles permitem comparar os caminhos de execução, sem concluir antecipadamente
que o Dynarec causa o sinal. Os comandos não alteram a configuração persistente
e não precisam baixar novamente o programa.

O launcher aceita `BOX64_LOG` de 0 a 3 e `BOX64_NOBANNER`, `BOX64_DYNAREC`,
`BOX64_SHOWSEGV`, `BOX64_SHOWBT`, `BOX64_X11THREADS` com 0 ou 1, e
`BOX64_ROLLING_LOG` de 0 a 2048. Sem opções explícitas, os logs
continuam desativados e o modo de emulação mantém seu padrão. O caminho de
bibliotecas continua vindo de `config-box64.json`.

Após ativar a voz, confira o nome exato:

```bash
timeout 90 "$HOME/.voicepeak-termux/bin/voicepeak-termux-box64-x11fix" --list-narrator
```

O backend aceita `重音テト`, `Kasane Teto` ou `Teto`. Sem esse inventário e um
WAV real validado, não configure o bot como Teto pronta.

## Português e worker

Teste a síntese na raiz do kit, usando o narrador que seu programa retornar:

```bash
export PHONE_WORKER_TETO_ENABLED=true
export PHONE_WORKER_VOICEPEAK_COMMAND="$HOME/.voicepeak-termux/bin/voicepeak-termux-box64-x11fix"
export PHONE_WORKER_VOICEPEAK_URL=
export PHONE_WORKER_VOICEPEAK_NARRATOR=重音テト
export PHONE_WORKER_VOICEPEAK_TEXT_MODE=ptbr-kana
export PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS=30
python deploy/voicepeak-teto/validate.py --render-test --timeout 120 --output "$HOME/teto-voicepeak-box64-teste.wav"
```

Ouça o WAV e meça o tempo. Português continua sendo uma aproximação em kana
para a voz japonesa. Se a síntese e a latência forem adequadas, use no ambiente
persistente do worker, `~/.phone-worker.env`:

```bash
PHONE_WORKER_TETO_ENABLED=true
PHONE_WORKER_TETO_BACKEND=voicepeak
PHONE_WORKER_VOICEPEAK_COMMAND="$HOME/.voicepeak-termux/bin/voicepeak-termux-box64-x11fix"
PHONE_WORKER_VOICEPEAK_URL=
PHONE_WORKER_VOICEPEAK_NARRATOR=重音テト
PHONE_WORKER_VOICEPEAK_TEXT_MODE=ptbr-kana
PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS=30
PHONE_WORKER_VOICEPEAK_STATUS_CACHE_SECONDS=60
PHONE_WORKER_VOICEPEAK_CACHE_REVISION=termux-box64-v040-xputpixel1-teto-1
PHONE_WORKER_VOICEPEAK_ALLOW_OTHER_VOICES=false
```

Reinicie pelo supervisor:

```bash
bash ~/.core-worker-runtime/current/start-phone-worker.sh --force-restart
```

Alinhe os prazos do worker e da VPS somente depois de medir a síntese, como
descrito em [TERMUX.md](TERMUX.md). O toolkit fica fora da release limitada do
worker; por isso o ZIP do updater e o kit do telefone são separados.

Fontes: [Box64 v0.4.0](https://github.com/ptitSeb/box64/tree/v0.4.0),
[compilação em Termux/PRoot](https://github.com/ptitSeb/box64/blob/v0.4.0/docs/COMPILE.md),
[variáveis do Box64](https://github.com/ptitSeb/box64/blob/v0.4.0/docs/USAGE.md),
[PRoot Distro](https://github.com/termux/proot-distro),
[programa oficial](https://www.ah-soft.com/voice/setup/).
