# VOICEPEAK no Poco via Termux — tentativa experimental

No Poco, o teste direto de `ldconfig.real` sob QEMU apresentou SIGSEGV.
A alternativa atual é [Ubuntu 24.04 ARM64 + Box64](BOX64.md), que deixa os
comandos e bibliotecas do Ubuntu nativos. O roteiro QEMU abaixo continua
documentado para comparação e recuperação do container existente.

O caminho preparado é:

`worker/Python ARM64 no Termux → launcher → PRoot Ubuntu x86_64 → QEMU → VOICEPEAK → WAV`

O motor usa a memória e a CPU do próprio telefone. O Python do worker continua
nativo; somente o programa Linux x86_64 passa por emulação. Não precisa de
root. A compatibilidade e a velocidade ainda não foram verificadas no Poco.
Não execute esta preparação dentro de outro PRoot: comece no Termux nativo.

## Colocar o kit no telefone

O ZIP incremental do updater atualiza o bot e o runtime do worker. Este toolkit
fica fora da release limitada do worker, por isso use também o ZIP separado
`teto-voicepeak-termux-kit-v3.zip` no telefone. Ele contém somente scripts e as
bibliotecas Python da integração; não contém o programa comercial nem vozes.

Baixe o kit para Downloads. No Termux nativo:

```bash
pkg install -y python unzip
termux-setup-storage
mkdir -p "$HOME/voicepeak-termux-kit"
unzip -o "$HOME/storage/downloads/teto-voicepeak-termux-kit-v3.zip" -d "$HOME/voicepeak-termux-kit"
cd "$HOME/voicepeak-termux-kit"
```

Aceite o acesso ao armazenamento quando o Android pedir; o programa e a
instalação Linux ficam na pasta privada do Termux, não em Downloads. Se o
Android tiver renomeado o ZIP baixado, ajuste o nome no comando `unzip`.

## Primeiro teste, sem comprar a voz

Na raiz do repositório atualizado, execute:

```bash
bash deploy/voicepeak-teto/termux/setup.sh
python deploy/voicepeak-teto/termux/fetch-engine.py
python deploy/voicepeak-teto/termux/diagnostic.py --probe-system --probe-runtime --timeout 60
```

O setup instala os pacotes Termux necessários e prepara um Ubuntu 22.04
**x86_64** dedicado, chamado `voicepeak-x64`. Ele preserva containers existentes
e não altera o ambiente do phone worker. Reserve alguns GB de armazenamento
livre para Ubuntu, bibliotecas, programa e futuras vozes.

O segundo comando baixa o pacote **oficial e público 1.2.22**, aproximadamente
194 MiB, seleciona o ZIP Linux e verifica seu SHA-256. É uma versão fixa para
teste de compatibilidade; o downloader oficial pode oferecer uma versão mais
recente. O pacote contém o programa, fontes e dicionários, **sem a voz Teto ou
uma licença**. Não é uma edição gratuita da Teto.

O programa é instalado em `~/.voicepeak-termux/engine/Voicepeak/`, que o
launcher apresenta ao guest como `/opt/Voicepeak/`. Esse diretório precisa
permanecer inteiro e gravável. O script recusa sobrescrever uma instalação
existente, preservando ajustes e possíveis ativações.

O diagnóstico verifica arquitetura, páginas de memória, RAM disponível,
armazenamento, QEMU, guest e abertura do `--help`. Ele também tenta o inventário
de narradores com prazo limitado. Também verifica o estado de `libc-bin` e do
cache de bibliotecas, sem corrigir pacotes durante o diagnóstico. Sem ativação, o inventário pode ficar
indisponível ou expirar; **isso não deve ser apresentado como Teto pronta**.

Para conferir só a abertura do programa:

```bash
timeout 60 "$HOME/.voicepeak-termux/bin/voicepeak-termux" --help
```

Se esse teste falhar, guarde o diagnóstico e a saída do terminal. Não compre a
voz contando com funcionamento no aparelho antes de resolver a abertura. Se
funcionar, ainda precisamos verificar a interface, a ativação e a síntese real.
Para o próximo diagnóstico, compartilhe os campos `page_size_bytes`,
`guest_architecture`, `engine_elf_x86_64`, `cli_help_ok` e `runtime_error`, se
existir. `teto_inventory_ok=false` é esperado sem voz instalada e ativada.

## Retomar após o erro de libc-bin / QEMU

Se o terminal mostrou `qemu: uncaught target signal 11` durante o trigger de
`libc-bin`, o setup anterior abortou antes de gravar a configuração nativa.
O kit v2 grava o launcher e o diagnóstico antes dos pacotes do guest. Ele
também reconhece `sys.platform="android"` no instalador do programa.
Esse ajuste corrige a rejeição do Python Android; não resolve por si só o
SIGSEGV do QEMU.

Atualize os scripts extraindo o ZIP v3 com `unzip -o`, como acima, e mantenha
o container existente. Na raiz do kit:

```bash
bash deploy/voicepeak-teto/termux/setup.sh --prepare-only
python deploy/voicepeak-teto/termux/recover-runtime.py --repair --timeout 180
python deploy/voicepeak-teto/termux/diagnostic.py --probe-system --timeout 60
```

`--prepare-only` prepara os comandos nativos e preserva a configuração, sem
executar `apt` nem reconfigurar pacotes dentro do Ubuntu. O reparo testa o
`ldconfig` real antes de chamar `dpkg --configure -a`. Se a varredura normal
falhar mas a varredura com `--ignore-aux-cache` funcionar, ele tenta reconstruir
o cache com o próprio `ldconfig -i`. Se ambas falharem, o reparo para e deixa o
container para investigação. Não troca `ldconfig` por um comando vazio nem
ignora o erro do `dpkg`.

O diagnóstico compara varreduras de bibliotecas sem atualizar links ou caches
(`-N -X`, com e sem `-i`) e registra somente estados e códigos de saída. Ele
também testa o cache e a varredura sem cache auxiliar com `PROOT_NO_SECCOMP=1`, sem persistir essa variável nem
aplicá-la automaticamente ao worker. Esse teste serve para investigar a
interação com PRoot; um resultado positivo isolado não comprova reparo.

Se o reparo retornar `recovered=true`, retome a instalação normal:

```bash
bash deploy/voicepeak-teto/termux/setup.sh &&
python deploy/voicepeak-teto/termux/fetch-engine.py &&
python deploy/voicepeak-teto/termux/diagnostic.py --probe-system --probe-runtime --timeout 60
```

O download anterior malsucedido era temporário e foi removido pelo instalador;
sem um ZIP oficial salvo à parte, o programa precisará ser baixado novamente.
Se o reparo não funcionar, envie os JSONs de reparo e diagnóstico. Não remova
o Ubuntu existente nem compre a voz antes de confirmar a abertura do programa.
Os testes do kit simulam falhas e estados de pacotes; este reparo ainda não
foi validado no Poco.

## Quando ldconfig passa, mas dpkg continua falhando

`code=0` nas varreduras e `libc_bin_state=half-configured` não identificam a
causa da falha do pós-instalação. O kit v2 descartava a mensagem de erro do
`dpkg`; no v3, `recover-runtime.py` preserva em `dpkg_error_output` as últimas
40 linhas, até 8192 caracteres, dessa falha específica de pacotes Ubuntu.
Ele continua marcando `recovered=false` enquanto a configuração falhar.

Para mostrar o erro diretamente, usando o container existente:

```bash
proot-distro login voicepeak-x64 -- /usr/bin/env LANG=C LC_ALL=C /usr/bin/dpkg --configure -a
proot-distro login voicepeak-x64 -- /usr/bin/env LANG=C LC_ALL=C /sbin/ldconfig.real -p
```

O primeiro comando repete a configuração dos pacotes pendentes com o erro
visível. O segundo apenas lê o cache usando o executável real do Ubuntu.
Compartilhe a saída para distinguir falha do pós-instalação, falha intermitente
sob QEMU e diferenças no cache. O VOICEPEAK não é executado por esses comandos.

No kit v3, o diagnóstico detalhado também compara o wrapper e o binário real
sem alterar pacotes ou caches:

```bash
python deploy/voicepeak-teto/termux/diagnostic.py --probe-system --probe-details --timeout 90
```

O campo `system_probe.details` informa bytes recebidos em stdout, contagem do
header do cache, quantidade de entradas e se `--version` retornou o conteúdo
esperado do `ldconfig`. Uma saída vazia, um cache com zero entradas e um formato
desconhecido ficam distinguíveis. Esses detalhes não tornam o sistema saudável
nem confirmam a execução da Teto.

## Interface gráfica e ativação oficial

A primeira instalação da voz precisa da interface e de um código de ativação
válido. Use [Termux:X11](https://github.com/termux/termux-x11), incluindo seu APK
e o pacote complementar conforme o projeto oficial. No Termux nativo, inicie
o servidor X11 e depois abra o programa pelo launcher:

```bash
termux-x11 :1 &
DISPLAY=:1 "$HOME/.voicepeak-termux/bin/voicepeak-termux"
```

Abra o aplicativo Termux:X11 no Android. A partir da interface do VOICEPEAK,
siga o procedimento oficial de licença e instalação da Kasane Teto. Nenhum
script deste patch compra, ativa ou contorna a licença. Não envie o código de
ativação em diagnósticos ou mensagens.

Os ajustes do launcher ficam em `~/.voicepeak-termux/config.json`. O `display`
pode continuar vazio para tentar CLI sem interface, ou ser configurado como
`:1` se o programa exigir X11 também no uso pelo bot. Nesse caso, mantenha o
servidor Termux:X11 iniciado. A variável `DISPLAY` no comando acima serve para
abrir a GUI sem alterar o padrão persistente.

Após ativar e instalar a voz, confira o nome retornado:

```bash
timeout 60 "$HOME/.voicepeak-termux/bin/voicepeak-termux" --list-narrator
```

O backend aceita os nomes exatos da Teto `重音テト`, `Kasane Teto` ou `Teto`.
Use aquele que o seu programa retornar. A versão histórica para Raspberry Pi
não comprova compatibilidade com a Teto atual; este roteiro usa o programa
Linux x86_64 oficial.

## Testar português e conectar ao worker

Somente após o inventário confirmar a Teto, na raiz do repositório:

```bash
export PHONE_WORKER_TETO_ENABLED=true
export PHONE_WORKER_VOICEPEAK_COMMAND="$HOME/.voicepeak-termux/bin/voicepeak-termux"
export PHONE_WORKER_VOICEPEAK_URL=
export PHONE_WORKER_VOICEPEAK_NARRATOR=重音テト
export PHONE_WORKER_VOICEPEAK_TEXT_MODE=ptbr-kana
export PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS=30
python deploy/voicepeak-teto/validate.py --render-test --timeout 120 --output "$HOME/teto-voicepeak-teste.wav"
```

Troque o narrador pelo nome confirmado. Ouça o WAV e meça o tempo. Português
continua sendo uma aproximação em kana; a emulação não corrige a pronúncia do
modelo japonês.

Se a síntese funcionar e a latência for aceitável, configure no ambiente
persistente **do próprio worker**, em `~/.phone-worker.env`:

```bash
PHONE_WORKER_TETO_ENABLED=true
PHONE_WORKER_TETO_BACKEND=voicepeak
PHONE_WORKER_VOICEPEAK_COMMAND="$HOME/.voicepeak-termux/bin/voicepeak-termux"
PHONE_WORKER_VOICEPEAK_URL=
PHONE_WORKER_VOICEPEAK_NARRATOR=重音テト
PHONE_WORKER_VOICEPEAK_TEXT_MODE=ptbr-kana
PHONE_WORKER_VOICEPEAK_STATUS_CACHE_SECONDS=60
PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS=30
PHONE_WORKER_VOICEPEAK_CACHE_REVISION=termux-v1222-teto-1
PHONE_WORKER_VOICEPEAK_ALLOW_OTHER_VOICES=false
```

O arquivo acima é carregado pelo shell do supervisor; o `$HOME` usa o diretório
do usuário Termux, sem substituí-lo por outro caminho. Também pode escrever o
caminho absoluto completo se preferir.

O prazo de inventário padrão continua cinco segundos; a nova chave permite
`1..60` segundos, respeitando o orçamento restante da solicitação. Para render
lento, alinhe `PHONE_WORKER_JOB_TIMEOUT_SECONDS` e, **na VPS do bot**,
`TTS_TETO_WORKER_TIMEOUT_SECONDS`. O teste standalone permite até 120 segundos,
mas esperar tanto pode ser inadequado para uma conversa no Discord. Não aumente
o prazo do bot antes de medir a síntese real.

Ao trocar `guest_executable`/`engine_directory` ou atualizar a instalação da
voz, altere também `PHONE_WORKER_VOICEPEAK_CACHE_REVISION`, por exemplo para
`termux-v1222-teto-2`, e reinicie o worker. O launcher é um arquivo separado do
motor; a revisão evita reutilizar áudio anterior quando ajuda/inventário e
o próprio launcher continuam iguais.

Reinicie pelo supervisor instalado:

```bash
bash ~/.core-worker-runtime/current/start-phone-worker.sh --force-restart
```

O status deve informar worker `1.11.29`, backend `voicepeak` e Teto pronta.
O guard de memória/temperatura/bateria e o bloqueio durante manutenção continuam
valendo para esta síntese local. Não é necessário executar o bridge HTTP nem
usar a RAM da VPS.

## Se a emulação falhar ou ficar lenta

PRoot compartilha o kernel Android. O diagnóstico registra o tamanho real das
páginas, sem presumir 4 ou 16 KiB pelo modelo do Poco. Algumas diferenças de
syscalls, memória e ambiente gráfico podem impedir o programa de funcionar.
Modificar uma variável não muda o tamanho de página do kernel.

O próximo experimento seria Box64 em um guest ARM64 com glibc, especialmente
se o QEMU abrir o programa mas a síntese demorar. Não está instalado por este
patch. FEX não é a primeira opção para Android, e Box64 compilado diretamente
para Bionic não substitui o ambiente Linux/glibc. Caso só a ativação falhe,
precisamos distinguir esse erro de incompatibilidade do emulador.

Fontes: [PRoot-Distro](https://github.com/termux/proot-distro),
[pacotes oficiais do VOICEPEAK](https://www.ah-soft.com/voice/setup/),
[instalação e ativação](https://www.ah-soft.com/voice/manual/02_setup.html),
[Termux:X11 com PRoot](https://github.com/termux/termux-x11#using-with-proot-environment),
[Box64 em Termux/PRoot](https://github.com/ptitSeb/box64/blob/main/docs/COMPILE.md).
