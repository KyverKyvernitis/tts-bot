# Phone Worker 1.11.43 — recuperação dos túneis SSH

Patch incremental sobre a base com Music Agent **0.3.88** desta conversa.
O ZIP contém somente os arquivos modificados e este guia, nas pastas
originais `tts-bot-main/`. O Music Agent continua na versão 0.3.88.

## O que muda

- Os dois canais passam a compartilhar um limite persistente de quatro
  novas tentativas SSH a cada 35 segundos. Chamadas manuais a `reconnect`
  não zeram essa contagem.
- As falhas passam a esperar progressivamente entre dez e sessenta segundos,
  com pequena variação aleatória. A espera só é zerada depois de trinta
  segundos de prontidão autenticada dos túneis.
- Uma falha no ping HTTP da API não encerra um túnel cujo controle SSH ainda
  responde. Isso evita interromper transferências durante uma indisponibilidade
  da API. A falha continua aparecendo no diagnóstico.
- A VPS verifica se o telefone ainda responde e pode liberar portas reversas
  de sessões mortas. Essa política fica restrita ao usuário dos túneis.

O patch preserva a verificação da chave da VPS, as chaves de pareamento e os
tokens existentes. Não altera codec, bitrate, filtros ou buffers de áudio.

## Diagnóstico do incidente

O SSH da VPS está ativo desde 15/10, sem reinícios, com a porta 22 escutando
e a configuração aprovada por `sshd -t`. Os diagnósticos do telefone mostram
o worker local pronto e os dois túneis recebendo `Connection refused`.

A VPS tem `22/tcp LIMIT IN`. A implementação anterior, simulada com o ciclo
real de verificação de cinco segundos e falhas rápidas de SSH, inicia os dois
canais a cada dez segundos: seis tentativas por janela de trinta segundos.
Isso alcança o limite padrão do UFW e disputa a mesma cota com o Termius.
Os logs fornecidos não comprovam ainda que o firewall bloqueou esse IP.
O primeiro status após a exceção temporária ainda estava reconectando. No
diagnóstico posterior, antes de instalar este patch, o transporte voltou:
`ok: true`, ambos os canais conectados, `worker_ready: true`, `vps_ready:
true` e ping autenticado de aproximadamente 75 ms. Isso reforça a hipótese
do limite do UFW, sem substituir a confirmação pelos logs do firewall.
O histórico do canal bulk também contém falha ao reservar a porta reversa
18767, compatível com uma sessão anterior ainda usando a porta. A amostra
não identifica o processo que a ocupava.

O IP público observado pela VPS mudou de `45.186.80.189` para
`160.19.174.91`. Desligar e ligar a conexão pode renovar o endereço público
mesmo sem trocar manualmente de Wi-Fi para dados móveis.

Para procurar a confirmação do bloqueio na VPS:

```bash
sudo journalctl -k \
  --since '2026-10-23 17:30:00' \
  --until '2026-10-23 18:05:00' --no-pager \
  | grep -E 'UFW.*(LIMIT|BLOCK).*SRC=(160\.19\.174\.91|45\.186\.80\.189).*DPT=22' \
  | tail -n 60

sudo iptables -S ufw-user-input
sudo iptables -S ufw-user-limit
```

A ausência de linhas não descarta o bloqueio: o registro do UFW também tem
limite de frequência. As tentativas inválidas de outros IPs nos logs do SSH
não comprovam invasão nem explicam, por si, a recusa da conexão do telefone.

## Recuperar o acesso enquanto instala

Mantenha a sessão SSH atual aberta. Se o IP público atual do telefone ainda
for `160.19.174.91`, a exceção abaixo permite SSH desse IP antes da regra
geral de limite. Ela não desliga o firewall nem altera a autenticação.
Execute uma única vez; não duplique a regra se já a adicionou.

```bash
sudo ufw insert 1 allow proto tcp from 160.19.174.91 to any port 22 comment 'osaka-phone-current-ip'
sudo ufw status numbered
```

No Termux:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py ensure
python ~/phone-worker/phone_worker_runtime/ssh_transport.py status
```

A exceção é temporária e depende desse endereço. O patch reduz as tentativas
de reconexão, mas não garante acesso através de um bloqueio externo, de um
firewall com política diferente ou de um IP compartilhado gerando tráfego
SSH adicional.

## Instalar na VPS

Pare a reprodução e aplique o ZIP pelo updater habitual. O conteúdo de
`tts-bot-main/` deve ser mesclado em `/home/ubuntu/bot`. Preserve os arquivos
de configuração, o staging dos arquivamentos e os dados do worker.

Se usar instalação manual, coloque o ZIP em `~/patch-worker-1.11.43-incremental.zip`:

```bash
unzip -l ~/patch-worker-1.11.43-incremental.zip

tar -czf ~/backup-worker-antes-1.11.43.tar.gz -C /home/ubuntu/bot \
  deploy/oci/ssh_phone_vps.py \
  deploy/termux/phone-worker/phone_worker.py \
  deploy/termux/phone-worker/phone_worker_runtime/ssh_transport.py

patch_dir=$(mktemp -d /tmp/patch-worker-1.11.43.XXXXXX)
unzip -q ~/patch-worker-1.11.43-incremental.zip -d "$patch_dir"
cp -a "$patch_dir/tts-bot-main/." /home/ubuntu/bot/
```

Atualize a configuração gerenciada do usuário de túneis. Este comando
reutiliza o cadastro existente, valida a configuração efetiva e recarrega
o SSH somente se necessário. A sessão administrativa atual fica aberta.

```bash
cd /home/ubuntu/bot
sudo cp -an /etc/ssh/sshd_config.d/90-osaka-phone.conf /etc/osaka/90-osaka-phone.conf.antes-1.11.43
sudo /usr/bin/python3 deploy/oci/ssh_phone_vps.py refresh --worker-id phone-localhost-c1111fd9
sudo /usr/sbin/sshd -t
sudo systemctl is-active ssh.service
```

Confira a política efetiva do usuário dos túneis:

```bash
sudo /usr/sbin/sshd -T \
  -C user=osaka-phone-ssh,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22 \
  | grep -E '^(clientaliveinterval|clientalivecountmax|authenticationmethods|passwordauthentication|gatewayports) '
```

Esperado: `clientaliveinterval 15`, `clientalivecountmax 2`,
`authenticationmethods publickey`, `passwordauthentication no` e
`gatewayports no`. Os keepalives aplicam-se às novas conexões desse usuário.
Eles permitem liberar as portas reversas de sessões mortas em dezenas de
segundos. Isso trata uma possibilidade de portas presas após a queda; os
logs fornecidos não confirmaram essa causa no incidente.

Se o updater não publicou o runtime, ou se instalou manualmente:

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python - <<'PY'
from dotenv import load_dotenv
import runpy
import sys

load_dotenv('/home/ubuntu/bot/.env', override=False)
sys.argv = ['scripts/core-worker-automation.py', 'after-update', '--force-agent']
runpy.run_path('scripts/core-worker-automation.py', run_name='__main__')
PY
```

## Atualizar o telefone e conferir

Se o telefone continua sem túnel, ele não consegue buscar a nova release pelo
bootstrap. Nesse caso, baixe o mesmo ZIP incremental na pasta Downloads do
telefone e aplique somente o auxiliar de transporte persistente. Este passo
não importa o worker, não executa o conteúdo do ZIP e não toca nos arquivos
de pareamento. Faz backup e substitui o módulo por renomeação atômica:

```bash
python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import tempfile
import zipfile

archive = Path.home() / 'storage/downloads/patch-worker-1.11.43-incremental.zip'
target = Path.home() / 'phone-worker/phone_worker_runtime/ssh_transport.py'
member = 'tts-bot-main/deploy/termux/phone-worker/phone_worker_runtime/ssh_transport.py'
if any(p.is_symlink() for p in (target, *target.parents)):
    raise SystemExit('Caminho do auxiliar contém link; confira a instalação antes de substituir.')
if not target.is_file():
    raise SystemExit('Auxiliar persistente não encontrado; confira o caminho da instalação.')
with zipfile.ZipFile(archive) as source:
    info = source.getinfo(member)
    if info.file_size > 256 * 1024:
        raise SystemExit('Tamanho inesperado do auxiliar.')
    raw = source.read(member)
compile(raw, member, 'exec')
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup = target.with_name(target.name + '.antes-1.11.43-' + stamp)
shutil.copy2(target, backup)
fd, temporary = tempfile.mkstemp(prefix='.ssh-recovery-', dir=target.parent)
try:
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), 0o644)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
finally:
    Path(temporary).unlink(missing_ok=True)
print('Auxiliar atualizado:', target)
print('Backup:', backup)
PY

python ~/phone-worker/phone_worker_runtime/ssh_transport.py ensure
```

O watchdog passará a usar o novo auxiliar na próxima verificação. A versão
geral do worker pode continuar 1.11.42 até o bootstrap conseguir atualizar.
Se o túnel continuar indisponível, colete `diagnostic` novamente; o patch não
contorna uma porta 22 recusada pela rede ou pela VPS.

Com o túnel acessível e o runtime publicado, execute no Termux:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --status
```

Confirme a versão efetiva no arquivo instalado:

```bash
python - <<'PY'
from pathlib import Path
import re

p = Path.home() / '.core-worker-runtime/current/phone_worker.py'
text = p.read_text()
print('runtime:', p.resolve().parent)
print('worker:', re.search(r'^PHONE_WORKER_VERSION\s*=\s*[\"\']([^\"\']+)', text, re.M)[1])
PY
```

Esperado: **1.11.43**. A versão do arquivo confirma a instalação; a versão
anunciada no painel do Discord confirma que o novo processo está rodando.
O `source_hash` esperado para o runtime publicado é:

```text
7cb35e93e1bf42b1bbef498dd46841f1105867d42fac05c431de56b5fbee0c27
```

Reconecte os túneis uma vez para aplicar os keepalives nas novas sessões:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py reconnect
python ~/phone-worker/phone_worker_runtime/ssh_transport.py ensure
```

Aguarde a negociação e consulte, sem executar reconexões repetidas:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py status
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

O transporte deve chegar a `state: connected`, `worker_ready: true` e
`vps_ready: true`. No Music Agent: `version: 0.3.88`, `available: true` e
`discord_ready: true`. O comando `reconnect` atua apenas nos túneis; não
reinicia a música nem o worker. A ativação de uma nova release pelo bootstrap
pode reiniciar o processo do worker.

## Testar queda e retorno da internet

1. Confirme o worker online no painel e uma reprodução normal antes do teste.
2. Pare a reprodução. Salve o diagnóstico inicial:

```bash
mkdir -p ~/music-start-tests
python ~/phone-worker/phone_worker_runtime/ssh_transport.py diagnostic \
  --output ~/music-start-tests/ssh-1.11.43-antes.json
```

3. Desligue a conexão do telefone por cerca de um minuto e ligue novamente.
   Deixe o Termux aberto. Não execute `reconnect`, `restart` ou `ensure`
   durante esta etapa: o teste deve observar a recuperação automática.
4. Aguarde até três minutos e consulte:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py status
python ~/phone-worker/phone_worker_runtime/ssh_transport.py diagnostic \
  --output ~/music-start-tests/ssh-1.11.43-depois.json
python ~/testar-music-agent.py status
```

Critérios: os dois canais conectados, `vps_ready` verdadeiro, `last_ok_at`
atualizado e painel do Discord online sem intervenção. O prazo de três
minutos é uma janela de teste, não uma garantia para qualquer rede.

5. Reproduza uma música arquivada e uma URL online. Escute pelo menos vinte
   segundos de cada uma, confira pausa/retomada e procure novos
   `music_first_packet_sent`, seguidos de `music_start_outcome` com
   `outcome: success`.
   O patch não altera codec, bitrate, filtros nem o caminho de áudio.
6. Se o primeiro ciclo passar, repita mais duas vezes, deixando a conexão
   estável por pelo menos um minuto entre ciclos.

Se continuar offline, capture também o estado da VPS:

```bash
sudo journalctl -u ssh.service --since '10 minutes ago' --no-pager
sudo journalctl -k --since '10 minutes ago' --no-pager \
  | grep -E 'UFW.*(LIMIT|BLOCK).*DPT=22' | tail -n 60
sudo ss -lntp '( sport = :22 or sport = :18766 or sport = :18767 )'
```

Exporte os diagnósticos no Termux:

```bash
cp ~/music-start-tests/ssh-1.11.43-antes.json ~/storage/downloads/
cp ~/music-start-tests/ssh-1.11.43-depois.json ~/storage/downloads/
```

## Retirar a exceção temporária

Depois de confirmar a atualização e a recuperação, mantenha a sessão atual
aberta e remova somente a exceção que adicionou:

```bash
sudo ufw delete allow proto tcp from 160.19.174.91 to any port 22
sudo ufw status numbered
```

Confirme que `22/tcp LIMIT` continua presente. Abra uma segunda sessão do
Termius antes de encerrar a atual. Repita um ciclo de queda/retorno sem a
exceção para validar o comportamento com o limite original do firewall.

## Validação local desta entrega

Passaram 132 testes focados de transporte, política SSH, saúde, autenticação,
watchdog e publicação. Incluem a validação nativa `sshd -t`/`-T`, a preservação
da política administrativa, rollback de configuração e simulações de queda,
relógio regressivo, verificações lentas e indisponibilidade da API.

O runtime de 63 arquivos foi publicado e extraído com os bootstraps 1.0.0 e
1.0.1, respeitando o limite de 64 membros do ZIP. Foram conferidos CRC,
hashes, bytes, permissões e sintaxe Python 3.10. O procedimento de atualização
offline do auxiliar foi executado em diretório isolado, verificando backup
e preservação dos arquivos privados.

Esses testes não incluem uma sessão real de Discord ou uma queda da rede no
seu aparelho com a versão nova. A recuperação observada no diagnóstico do
usuário ocorreu antes da instalação deste patch.

## Rollback

Restaure os três arquivos de produção do backup ou use o rollback do updater
para a base 0.3.88. Republique o runtime anterior e use `--check` no telefone.
O script da base anterior não tem o comando `refresh`. Os keepalives já
aplicados podem permanecer na VPS durante o rollback do aplicativo. Para
restaurar também a política anterior do SSH, mantenha a sessão administrativa
aberta e use o backup feito antes da instalação:

```bash
sudo cp -a /etc/osaka/90-osaka-phone.conf.antes-1.11.43 /etc/ssh/sshd_config.d/90-osaka-phone.conf
sudo /usr/sbin/sshd -t
```

Somente se a validação terminar sem erro, recarregue:

```bash
sudo systemctl reload ssh.service
```

Preserve os arquivos privados
de pareamento, os tokens e os diretórios de estado. O transporte persistente
de recuperação pode conservar a versão mais nova após rollback; não o apague
durante uma recuperação remota. Para retornar também esse componente, copie
somente o `ssh_transport.py` anterior validado para o caminho persistente
usado pelo watchdog, com a conexão estabilizada e a sessão administrativa
aberta. A versão anterior volta a apresentar o excesso de tentativas descrito.
