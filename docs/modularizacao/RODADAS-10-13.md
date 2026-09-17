# Continuação — telemetria e consumo das medições

Data: 10/09/2026. Entrada: `4dd8e15b8fb5766b9a99fd44dd041d1872510f0f`.
SHA-256 do ZIP de código de entrada:
`a96f5c20b3a8849dd60ae256781a3a5fcd82cfa233b55f17b9305c587fa9e81f`.
Antes de editar, conferidos 561 arquivos por hash/modo e 221 entradas do envelope;
Git limpo. `rodada10-entrada-inventario.json` registra a inspeção. A skill específica
não estava disponível; foram seguidos os documentos equivalentes do checkpoint.

## Rodada 10 — contratos e bugs de bateria

Código: `_battery_snapshot`/`_sysfs_battery_snapshot` no facade e
`tests/test_phone_worker_telemetry.py`. Primeiro foram testados leitores de texto
reais em sysfs temporário e o leitor JSON real com subprocesso controlado. Há
permissões negadas, campos parciais, API ausente/timeout/JSON inválido, aliases,
clamping, status/plugged, unidades e comportamento do gate de recursos Teto.

`rodada10-bateria-entrada`: **17 failed / 26 passed**. Quatro grupos de bugs:
conversão de temperatura apenas acima de 1000; enumeração que descartava a bateria
já encontrada; JSON Termux não vazio sem medida tratado como disponível; valores
NaN/infinito que contaminavam o snapshot. Foram corrigidos antes da extração.
`rodada10-bateria`: **43 passed**; zero mutações de runtime/release.
Commit da correção: `93c119f`.

A unidade de `power_supply/temp` é décimo de grau Celsius, sem heurística de
magnitude, conforme a [documentação do kernel Linux](https://docs.kernel.org/power/power_supply_class.html).
O valor de temperatura Termux continua em graus como no contrato anterior. A
correção evita que 350 seja apresentado como 350 °C e bloqueie o gate Teto quando
o valor é 35 °C. O teste desse consumidor não sintetiza áudio nem mede latência.

Próximo passo da rodada: mover a lógica já testada e preservar os bindings e boot.

## Rodada 11 — extração de bateria

Código: novo `phone_worker_runtime/telemetry.py`, loader/wrappers no facade e
entrada nas listas de distribuição do runtime e do publisher. O loader tem um
lock de criação, cache por facade e caminho ao lado do entrypoint. Ausência/falha
de carga retorna dados indisponíveis; uma chamada posterior tenta novamente. Os
probes e a fábrica de fallback são passados na chamada, sem import circular.

Testes adicionais: carga concorrente, reatribuição de probes/função do módulo,
chegada tardia e import isolado sem writes, threads, processos ou rede. O publisher
gera o ZIP completo em tmp_path e o bootstrap original valida todos os membros.
Alterar somente telemetry.py muda hash, pacote e identidade reportada. Módulo
ausente impede preparar a release. O ZIP tem **27 membros** incluindo manifesto.

`rodada11-extracao`: **122 passed** (bateria/release/configuração/fronteiras/boot/TTS).
Depois, a enumeração de sysfs também passou a ser callback explícito no facade;
essa pequena alteração posterior ao gate 122 foi coberta nas rodadas 12–13 e no
gate global, sem atribuir a ela a fotografia anterior. Commit: `28ca2d2`.
Próximo passo: contratos de rede, cache e timeouts antes da próxima extração.

## Rodada 12 — rede, Tailscale e cache de ping

Código: `tests/test_phone_worker_network_telemetry.py`, correção de erro Wi-Fi no
facade e extração de `_vps_tcp_ping_snapshot`, `_tailscale_snapshot` e
`_network_snapshot` para o mesmo módulo. `_PING_CACHE` continua no facade, recebido
por referência em cada chamada. TTL, chave host/porta, cópias de resultado,
timeout mínimo, erros cacheados, fechamento TCP/HTTP e limites de leitura mantidos.
Clocks e probes são explícitos; nenhuma rede real ou job remoto foi utilizado.

`rodada12-rede-entrada`: **1 failed / 18 passed**. JSON contendo apenas erro de
permissão era anunciado como Wi-Fi. Após a recusa desse payload,
`rodada12-rede-correcao`: **19 passed**, commit `dc0048a`.
Após a extração, `rodada12-rede-extracao`: **141 passed**, incluindo regressões de
bateria, config, release, import/startup e TTS; commit `fc53a9e`.

Ausência do módulo mantém snapshots indisponíveis também para rede/ping/Tailscale.
A política preexistente de inferir app/VPN a partir da URL configurada foi mantida;
o campo connected nessa inferência não demonstra conectividade em aparelho. O
cache não ganhou um lock ou outro dono; concorrência de cache miss não foi redesenhada.
Próximo passo da rodada: revisar consumidores das medições antes do gate global.

## Rodada 13 — prontidão para tarefas pesadas

Código: `_assist_readiness_snapshot` e nove casos no teste de telemetria. Nível zero
virava medida ausente ou era substituído pelo alias percent; plugged não vazio
incluía strings unplugged/battery como carregamento, e o booleano charging podia
ser ignorado. Esses casos alteravam a recomendação de build pesado indevidamente.
O consumidor agora preserva zero, prioriza o booleano e usa status/plugged apenas
quando ele não existe. Medida ausente continua best-effort; tarefas leves e schema
do resultado permanecem.

`rodada13-assist-entrada`: **5 failed / 4 passed / 48 deselected**. Depois da
correção, `rodada13-assist`: **76 passed** (57 bateria/consumo/bindings e 19 rede).
Não foi enviado nem executado um job de build; são testes da recomendação local.

## Fechamento e pendências

O relatório externo associa o gate global, lint/sintaxe, commit e inventário exato
da entrega. Os JSONs de cada execução guardam comandos, horários, hashes de fontes,
exit e mutações; os logs não foram reescritos. O inventário AST será atualizado
antes do gate final. Os quatro JARs do host foram conferidos novamente por hash
e integridade ZIP: iguais aos usados na rodada 9; nenhuma dependência nova.

WorkerHandler, main, transporte TTS, Java de produção, Music, self-builder,
bootstrap de produção e dependências da VPS permanecem iguais à entrada. O gate
de recursos Teto consome a temperatura corrigida; não foi alterado seu código.
Não houve nova coleta no áudio nem medição de latência em aparelho. Não houve
Gradle/APK, instalação, publicação ou envio de jobs remotos.

A fonte Coptic ausente da base e o skip por visibilidade de `/proc` continuam
classificados no checkpoint anterior. As limitações Java/plataforma, discovery
dinâmico, promoção/rollback e identidade de processo em aparelho permanecem.

Próximo passo concreto: testes de autenticação/configuração do control plane e
envelope de `_core_worker_payload`, antes de extrair esse domínio. Manter mapas,
locks, outbox e threads com um único dono; preservar a VPS de 1 GB sem worker.
Depois Voice Agent, TTS com estado estabilizado, Music, self-builder e UI.
