# IP secundário para Edge TTS na OCI

Esta instalação preserva `10.0.0.237` na `ens3`, cuja origem principal é `10.0.0.174`.
O público reservado `147.15.16.46` já foi associado ao secundário na OCI e validado
com Edge; o público principal é `163.176.47.24`. A configuração na OCI continua
sendo responsabilidade do operador.

O diagnóstico anterior encontrou o secundário **já presente**. Também encontrou
duas rotas padrão preexistentes; ambas foram preservadas. A presença atual do IP
não comprova sua persistência após reboot.

## Comportamento no bot

Com `TTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237`, cada servidor Discord recebe uma
origem preferida por um hash estável do ID. Não há outro processo, worker ou bot.
Se a origem escolhida falhar ou estiver em cooldown, a outra origem local é
tentada; depois, Edge no Phone Worker; por último, gTTS. O cooldown de HTTP 429 e
o circuito de falhas são independentes por IP. Uma recuperação em outro IP não
limpa a penalidade do primeiro.

O streaming continua progressivo. Cada produtor mantém sua origem e seu próprio
buffer, enquanto o cache de áudio final continua compartilhável entre origens.
Os avisos de rate limit no Discord só são enviados após fallback real para gTTS,
com cooldown de três horas por servidor e exceção para aumento do nível.

Valor vazio mantém o comportamento anterior, usando a saída padrão do sistema.
A configuração afeta exclusivamente o transporte Edge; Teto e música mantêm suas
rotas e clientes existentes.

## Conferir antes de instalar

Na raiz do projeto na VPS:

```bash
python3 deploy/oci/ensure_secondary_ip.py inspect \
  --primary-ip 10.0.0.174 --secondary-ip 10.0.0.237 --interface ens3
```

A consulta lê endereços e rotas; não altera a rede. A busca por persistência
existente exibe somente nomes de arquivos, nunca conteúdos, senhas ou tokens.
Uma consulta sem root pode não conseguir ler todos os arquivos. Nesse caso,
repita usando `sudo -n /usr/bin/python3` no lugar de `python3`.

Referências em netplan, NetworkManager, networkd, cloud-init ou serviços próprios
são indícios de gestão existente. O instalador recusa criar uma segunda gestão
quando encontra essas referências. Confira o gerenciador ativo antes de seguir:

```bash
systemctl is-active systemd-networkd NetworkManager
networkctl status ens3 --no-pager
```

Para NetworkManager, consulte somente campos de endereço/perfil com `nmcli -g`;
não use `--show-secrets` nem publique arquivos de conexão completos. Se um perfil
persistente ativo já declara o secundário, mantenha essa gestão. Referências em
arquivos gerados sob `/run` isoladamente não provam persistência.

Se a referência externa tiver sido revisada e não gerenciar o endereço, a opção
`--allow-native-references` permite instalar a proteção de boot. Ela não altera
nem remove a configuração externa.

## Instalar a proteção de boot

```bash
bash deploy/oci/install-secondary-ip.sh \
  --primary-ip 10.0.0.174 --secondary-ip 10.0.0.237 --interface ens3
```

Esses valores já são os defaults. O wrapper usa `sudo -n` somente para o
instalador; não executa `sudo -v` nem pede senha. Para revisar todas as verificações
sem instalar arquivos, acrescente `--dry-run` ao comando acima.

O instalador valida a interface, o primário, referências externas, os marcadores
dos arquivos existentes e a unit antes de gravar. Os destinos são:

- `/usr/local/libexec/osaka-edge-secondary-ip.py`, root:root, modo 0755;
- `/etc/osaka-edge-secondary-ip.json`, root:root, modo 0644;
- `/etc/systemd/system/osaka-edge-secondary-ip.service`, root:root, modo 0644.

Arquivos existentes sem o marcador do instalador, links simbólicos, destinos
graváveis por outros usuários e unidades com drop-ins externos são recusados.
Somente esta unit é habilitada/iniciada; bot, interfaces e serviços de rede não
são reiniciados. Nenhum `.env` é escrito pelo instalador.

A unit aguarda o primário por até 30 segundos depois de `network-online.target`.
Se o secundário estiver na `ens3`, preserva também seu prefixo atual. Se estiver
ausente, executa somente:

```text
ip address add 10.0.0.237/32 dev ens3 noprefixroute
```

Um secundário em outra interface ou um primário ausente/ambíguo impede alterações.
O helper nunca executa `address del`, `flush`, `replace`, `route add`, `netplan
apply` ou `link down`. As duas rotas padrão são comparadas preservando duplicatas;
uma mudança externa é reportada, sem restaurar tabelas de roteamento.

Não há `ExecStop`: parar a unit não remove o endereço usado por conexões ativas.
A proteção cobre a inicialização. Um futuro reconfigure do gerenciador de rede
pode remover endereços adicionados externamente; nesse caso, revisar a gestão
nativa é necessário. Nenhum timer de reparo é instalado.

## Ativar as duas origens no TTS

Após instalar, ajuste somente esta variável no `.env` já existente do bot:

```dotenv
TTS_EDGE_SOURCE_IPS=10.0.0.174,10.0.0.237
```

O operador/updater deve aplicar a configuração no próximo restart controlado do
bot. Este instalador não faz esse restart. As conexões Edge precisam usar o
privado correspondente; endereços públicos não são endereços locais para bind.

No `/health` do bot, o objeto `tts_metrics.edge_sources` apresenta tentativas,
sucessos, falhas, HTTP 429, cooldown e estado do circuito para cada IP privado.
`tts_metrics.edge_sources_enabled` confirma se a lista explícita foi ativada, e
`tts_metrics.edge_sources_config_error` informa erros nessa configuração.

O Edge 7.3.1 consulta proxies de ambiente internamente. Se uma conexão vinculada
tentar usar um proxy, o conector recusa a tentativa com a mensagem
`proxy de ambiente incompatível com a origem Edge local explícita`. Isso evita
registrar o IP de um proxy como se fosse a origem configurada. O patch não altera
variáveis de ambiente ou clientes globais para Teto/música. Quando esse erro
ocorrer, confira a configuração efetiva de proxy do serviço antes de alterar
qualquer configuração compartilhada; não publique credenciais de proxy.
Com `TTS_EDGE_SOURCE_IPS=` vazio, o bot mantém a saída padrão do sistema. Isso
não remove o secundário nem desabilita a proteção de boot.

Verificação sem reiniciar a VPS:

```bash
sudo -n /usr/bin/python3 /usr/local/libexec/osaka-edge-secondary-ip.py inspect \
  --config /etc/osaka-edge-secondary-ip.json
systemctl is-enabled osaka-edge-secondary-ip.service
systemctl status osaka-edge-secondary-ip.service --no-pager
```

Isso confirma a configuração e a execução atual. A restauração após reboot só
fica comprovada quando um reboot autorizado for realizado e conferido.

Para desabilitar apenas a proteção de boot, preservando o endereço atual:

```bash
sudo -n systemctl disable --now osaka-edge-secondary-ip.service
```
