# Osaka: conexão direta com o Termux por SSH

O telefone inicia duas conexões SSH com o IP público da VPS. A Osaka continua
usando as APIs HTTP e o streaming existentes; as portas do worker ficam
acessíveis somente pelo loopback da VPS. O Tailscale não participa do fluxo
depois da ativação.

| Canal | Endereço na VPS | Destino no telefone |
| --- | --- | --- |
| TTS, Teto e comandos curtos | `http://127.0.0.1:18766` | Porta efetiva do worker, normalmente `8766` |
| Cache, arquivos e streams de música | `http://127.0.0.1:18767` | O mesmo worker |
| Retorno do telefone à VPS | Termux `http://127.0.0.1:19000` | VPS `http://127.0.0.1:10000` |

O Edge continua consultando a Microsoft pela conexão de Internet do telefone.
A autenticação HTTP e o ID do worker existente são mantidos.
No modo SSH, as rotas de manutenção `/local/*` também exigem o token existente
do worker, pois uma conexão encaminhada chega ao telefone pelo loopback.

## Antes de começar

- Aplique o ZIP incremental na raiz do projeto da VPS, normalmente
  `/home/ubuntu/bot`. Os comandos abaixo partem dessa pasta.
- Prepare também os fontes do telefone antes da ativação. A cópia inicial abaixo
  pode ser feita a partir do ZIP em Downloads, mesmo sem a VPN. O bootstrap
  instalará depois a release completa e validada pelo próprio túnel SSH.
- Tenha o **ID atual** do telefone, exibido no painel `workers`, e o IP público
  ou DNS atual da VPS. Não use o endereço privado `100.x` do Tailscale como
  destino SSH. Os IPs de relatórios antigos não são uma confirmação do IP atual.
- A porta SSH pública da VPS deve aceitar a conexão do telefone. O padrão é
  `22`; não é necessário abrir as portas HTTP `8766`, `18766`, `18767` ou `10000`
  na Internet. A instalação valida a configuração do OpenSSH antes de recarregá-la.
- Faça a ativação em uma janela sem fala, música ou job ativo. A troca de
  configuração precisa ser carregada pelo worker e pelo bot.

Na VPS, carregue o código novo e publique a release completa pelo mecanismo já
existente. Faça isso na janela sem reprodução; o primeiro reinício carrega também
a resposta de manifesto compatível com a origem local do túnel:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
sudo -n systemctl restart tts-bot
python3 scripts/core-worker-automation.py queue-agent-update --worker-id "$OSAKA_PHONE_ID"
```

O comando publica a release mesmo quando o telefone está offline. Se o canal
anterior ainda está disponível, a atualização automática pode concluir essa
transferência inicial; ele não será necessário depois da migração.

Para um **worker já instalado e pareado**, copie o ZIP incremental para Downloads
do telefone e execute este bloco no Termux. Ele copia somente os fontes do worker
e do runtime de música presentes no patch para a pasta canônica, preservando o
`.env`, as chaves e os dados existentes:

```bash
read -r -p 'Caminho do ZIP incremental em Downloads: ' OSAKA_SSH_ZIP
python - "$OSAKA_SSH_ZIP" <<'PY_INSTALL_SSH_SOURCES'
from pathlib import Path, PurePosixPath
import os, stat, sys, tempfile, zipfile
root = Path.home() / 'phone-worker'
copied = []
with zipfile.ZipFile(Path(sys.argv[1]).expanduser()) as archive:
    for member in archive.infolist():
        original = PurePosixPath(member.filename)
        if original.is_absolute() or '..' in original.parts or '\\' in member.filename:
            raise ValueError('caminho inválido no ZIP')
        if stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError('link recusado no ZIP')
        parts = original.parts
        if parts and parts[0] == 'tts-bot-main':
            parts = parts[1:]
        name = '/'.join(parts)
        if name.startswith('deploy/termux/phone-worker/'):
            relative = PurePosixPath(name).relative_to('deploy/termux/phone-worker')
        elif name.startswith('cogs/musica/runtime_telefone/') or name in {'cogs/__init__.py', 'cogs/musica/__init__.py'}:
            relative = PurePosixPath(name)
        else:
            continue
        if member.is_dir():
            continue
        if relative.name == '.env' or member.file_size > 2 * 1024 * 1024:
            raise ValueError('arquivo de instalação recusado')
        target = root.joinpath(*relative.parts)
        for parent in (target, *target.parents):
            if parent.is_symlink():
                raise ValueError('link recusado no destino')
            if parent == root:
                break
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.ssh-patch-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o755 if target.suffix == '.sh' else 0o644)
                output.write(archive.read(member))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        copied.append(str(relative))
required = root / 'phone_worker_runtime/ssh_transport.py'
if not required.is_file():
    raise RuntimeError('o ZIP não forneceu ssh_transport.py; confira o pacote')
print('Fontes preparados:', len(copied))
print('Transporte:', required)
PY_INSTALL_SSH_SOURCES
```

Esse passo não é uma instalação nova nem promove o ponteiro `current`. A release
completa será baixada na etapa 4. Não copie `.env`, chaves ou diretórios de estado
de outro aparelho. Para instalar em um Termux novo, use os fontes completos e o
instalador do worker antes de seguir este guia.

No Termux, confira as dependências locais:

```bash
python --version
ssh -V
test -f "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py"
```

Se `python` ou `ssh` não estiver instalado, instale somente as ferramentas ausentes
antes de continuar:

```bash
pkg install python openssh
```

O transporte usa OpenSSH e a biblioteca padrão do Python. Não exige `autossh`,
outro worker ou um serviço novo de HTTPS/enrollment.

## 1. Preparar a VPS

Execute na VPS. O wrapper usa `sudo -n`; não execute `sudo -v`.

```bash
cd /home/ubuntu/bot
read -r -p 'IP público ou DNS atual da VPS: ' OSAKA_SSH_HOST
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
bash deploy/oci/ssh-phone-vps.sh prepare \
  --host "$OSAKA_SSH_HOST" \
  --worker-id "$OSAKA_PHONE_ID" \
  > "$HOME/osaka-ssh.json"
cat "$HOME/osaka-ssh.json"
```

O resultado informa `ok: true`, um objeto `bundle` e a impressão digital da
chave pública do servidor. Guarde o JSON como `osaka-ssh.json` em **Downloads do
telefone**, usando a sessão administrativa da VPS que você já conhece. O Termux
aceita o resultado completo do comando ou apenas o objeto `bundle`.

Esse arquivo contém os endereços, as portas e a chave **pública** da VPS. A chave
privada do servidor não deve ser transferida. O destino e a identidade do
servidor serão fixados a partir desse arquivo; a instalação não aceita uma chave
de host desconhecida silenciosamente.

Se a VPS usa outra porta pública de SSH, acrescente `--ssh-port PORTA` ao
`prepare`. As demais portas têm os padrões da tabela e são configuradas pelo
mesmo pacote. Uma porta ocupada ou uma configuração diferente já cadastrada
deve ser investigada antes de repetir a preparação.

## 2. Preparar o Termux e gerar sua chave pública

No Termux, com o arquivo da etapa anterior em Downloads:

```bash
python "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py" prepare \
  --bundle "$HOME/storage/downloads/osaka-ssh.json"
```

O comando valida o pacote e prepara a chave Ed25519 exclusiva do transporte.
Copie a linha pública que começa com `ssh-ed25519` exibida no resultado para a
etapa seguinte. A chave privada permanece na pasta privada do Termux.

Se o Android não permitir abrir Downloads, execute `termux-setup-storage`,
autorize o acesso aos arquivos e repita o comando. A preparação não exige um
novo pareamento nem altera as vozes e os tokens do worker existente.

## 3. Cadastrar a chave pública na VPS

Na VPS, cole **somente a linha pública** gerada pelo telefone:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
read -r -p 'Cole a linha ssh-ed25519 pública do telefone: ' OSAKA_PHONE_PUBLIC_KEY
bash deploy/oci/ssh-phone-vps.sh enroll \
  --worker-id "$OSAKA_PHONE_ID" \
  --public-key "$OSAKA_PHONE_PUBLIC_KEY"
```

O resultado deve informar `ok: true` e a impressão digital da chave cadastrada.
A conta SSH dedicada permite apenas os encaminhamentos configurados, sem shell,
PTY, sudo ou encaminhamento de agente.

## 4. Ativar, validar e carregar a configuração

Pause as reproduções e os jobs antes desta etapa. No Termux:

```bash
python - <<'PY_PRIVATE_ENV'
import os, stat
from pathlib import Path
path = Path(os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env').expanduser()
fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise RuntimeError('O .env precisa ser arquivo regular do usuário atual, com um único link.')
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
print('Permissões privadas do .env verificadas; conteúdo preservado.')
PY_PRIVATE_ENV
python "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py" activate --apply-worker
python "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py" status
```

A verificação acima aceita o caminho de `PHONE_WORKER_ENV`, quando configurado,
e restringe o `.env` a `0600` sem ler ou exibir seu conteúdo. Links simbólicos,
arquivos de outro proprietário e arquivos com múltiplos hard links são recusados.

A ativação valida o transporte antes de aplicar as URLs locais. Com
`--apply-worker`, o bootstrap baixa e valida a release completa usando
`127.0.0.1:19000` e carrega a configuração no processo do worker durante esta
migração explícita. Uma release já atual não é baixada novamente; o carregamento
do worker evita um segundo reinício quando o bootstrap já concluiu a troca.
A reconexão normal do túnel não reinicia o worker ou o
Music Agent. Se a ativação indicar trabalho ativo ou alguma validação pendente,
resolva a condição informada e repita a etapa; não encerre processos por nome.

Depois, na VPS:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
bash deploy/oci/ssh-phone-vps.sh activate \
  --worker-id "$OSAKA_PHONE_ID" \
  --env-file /home/ubuntu/bot/.env
```

Esse comando exige que os dois canais confirmem o mesmo worker Termux, Edge
disponível e protocolo de streaming compatível. Ele grava apenas as opções de
transporte e conserva os tokens, os ajustes de voz e `TTS_EDGE_SOURCE_IPS`.
O bot ainda precisa carregar o `.env` atualizado. Na janela sem reprodução:

```bash
sudo -n systemctl restart tts-bot
systemctl show tts-bot --property=ActiveState,SubState --no-pager
```

Abra o painel `workers` e confira **Conectado via SSH**, a confirmação recente e
a disponibilidade de Edge/Teto. A existência de um PID SSH ou de uma porta local
aberta, isoladamente, não comprova que o outro lado está respondendo.

Para consultar o cadastro na VPS:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
bash deploy/oci/ssh-phone-vps.sh status --worker-id "$OSAKA_PHONE_ID"
```

Após confirmar os canais e a atualização, desligue o Tailscale e repita os testes
de fala e música. As novas URLs, o heartbeat, o bootstrap e os downloads devem
continuar funcionando sem ele.

## Medir velocidade sem gerar dezenas de sínteses

O benchmark roda **na VPS**, lê o token do `.env` local e salva relatórios `.json`
e `.txt`. O teste padrão é somente de health, sem nova síntese:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
python3 deploy/oci/benchmark_phone_ssh.py \
  --worker-id "$OSAKA_PHONE_ID" \
  --samples 50 \
  --output "$HOME/osaka-ssh-benchmark"
```

Para medir também o primeiro áudio do cache do telefone, informe um texto já
cacheado com a voz indicada. Um cache miss interrompe a amostragem de áudio;
não autoriza sínteses de reposição:

```bash
python3 deploy/oci/benchmark_phone_ssh.py \
  --worker-id "$OSAKA_PHONE_ID" \
  --samples 50 \
  --audio-text 'Teste de diagnóstico Edge TTS da Osaka.' \
  --voice pt-BR-FranciscaNeural \
  --output "$HOME/osaka-ssh-audio"
```

Se for necessário criar esse áudio para a comparação, acrescente `--warm-audio`.
Essa opção permite **uma única requisição Edge de aquecimento**, seguida de
requisições que aceitam somente cache. Os dois caminhos devem alcançar o mesmo
worker e o mesmo processo; o benchmark recusa uma comparação entre telefones
diferentes.

Antes do corte definitivo, uma comparação temporária pode acrescentar
`--baseline-base http://ENDERECO_PRIVADO_ANTERIOR:8766`. Use o endereço que
realmente funciona no ambiente, não esse texto literal. O Tailscale é opcional
apenas para essa medição de referência; não é uma reserva operacional do SSH.

Compare p50/p95 de health e primeiro áudio cacheado, incluindo erro e cobertura
das amostras. A meta é reduzir o p95 em 10%; a migração prioriza independência
sem regressão relevante acima de 5%. Um relatório incompleto ou sem a rota de
referência não comprova ganho sobre o Tailscale. Testes de transporte também não
substituem a validação de Edge/Teto novos e da reprodução no Discord.

## Diagnóstico e recuperação

No Termux, para gerar um relatório sanitizado em Downloads:

```bash
python "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py" diagnostic \
  --output "$HOME/storage/downloads/osaka-ssh-diagnostico.json"
```

Para pedir que o supervisor refaça os canais SSH sem reiniciar o worker:

```bash
python "$HOME/phone-worker/phone_worker_runtime/ssh_transport.py" reconnect
```

O watchdog mantém o transporte antes de consultar as atualizações. As chaves,
a configuração e os launchers canônicos em `~/phone-worker` ficam fora do
ponteiro de release `current`, para conservar a recuperação durante updates e
rollback do aplicativo.

Na VPS, o rollback do instalador restaura **somente as opções de transporte da
base compatível com SSH**, preservando as outras configurações atuais:

```bash
cd /home/ubuntu/bot
read -r -p 'ID atual do telefone: ' OSAKA_PHONE_ID
bash deploy/oci/ssh-phone-vps.sh rollback \
  --worker-id "$OSAKA_PHONE_ID" \
  --env-file /home/ubuntu/bot/.env
```

Esse rollback não reativa URLs Tailscale nem volta o código inteiro do bot.
Se o resultado indicar `bot_restart_required`, carregue a configuração durante
uma janela sem reprodução. O backup anterior à migração existe para inspeção
administrativa; não é aplicado automaticamente pelo fluxo SSH.

Se uma chave de host mudar, confira a nova identidade diretamente na VPS antes
de preparar um pacote atualizado. Não desative a validação de host para contornar
o erro. Se a conexão SSH pública não abre, confira a rede do telefone e as regras
da porta SSH da VPS; abrir portas HTTP públicas não resolve o encaminhamento.

No Android, habilite inicialização automática e bateria **Sem restrições** para
Termux, Termux:Boot e Termux:API quando utilizada. Abra o Termux:Boot uma vez após
instalá-lo. Se o Android encerrar todos os processos do Termux, o painel pode
orientar abrir o aplicativo, mas a VPS não consegue acordar um túnel inexistente.

## O que não faz parte do pacote

O ZIP incremental contém somente fontes e arquivos de suporte alterados. Não
contém `.env`, tokens, chaves privadas, `known_hosts` do aparelho, sockets de
controle ou diretórios de estado. As versões e os resultados efetivamente
testados devem ser conferidos no relatório de validação entregue com o patch.
