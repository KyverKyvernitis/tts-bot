# Updater

Implementação canônica do atualizador transacional do bot. Todo código, infraestrutura,
integração Discord, utilitários e testes exclusivos do updater vivem nesta pasta.

## Visão geral

O fluxo normal começa quando um ZIP é recebido no Discord. A integração em `discord/`
valida o pacote e grava um candidato na fila. O `bot-updater.path` observa a fila e
aciona o serviço systemd imediatamente; o timer fica apenas como fallback. O serviço
executa `core/atualizar.sh`, que cria uma cópia runtime estável do próprio updater,
carrega os módulos do `core/` e conduz a atualização em worktree isolado antes de
promover qualquer mudança para a árvore principal.

As dez macroetapas apresentadas no Discord são:

1. Pacote
2. Segurança
3. Preparação
4. Isolamento
5. Validação
6. Release
7. Promoção
8. Aplicação
9. Verificação
10. GitHub

As durações são acumuladas por etapa e a progressão é monotônica: eventos atrasados
não podem fazer o card voltar para uma fase anterior.

## Estrutura

### `core/`

- `atualizar.sh`: bootstrap, locks, traps e carregamento dos módulos.
- `configuracao.sh`: configuração operacional e perfis de prioridade.
- `estado.sh`: estado runtime da execução.
- `git.sh`: operações Git e snapshots transacionais.
- `registros.sh`: logs, evidências, incidentes e alertas técnicos.
- `tempos.sh`: medição e formatação de tempos.
- `fila.sh`: fila de candidatos, claim, arquivamento e dispatch.
- `manutencao.sh`: retenção, limpeza e proteção de espaço em disco.
- `persistencia.sh`: estado durável do candidato e dados de recovery.
- `progresso.sh`: progresso, histórico e entrega visual ao Discord.
- `validacao.sh`: preflight, health checks e validação de candidatos.
- `candidato.sh`: worktree, artefatos, runtimes, commit e promoção local.
- `mudancas.sh`: diff, classificação de impacto e proteção da árvore local.
- `aplicacao.sh`: deploy, systemd, releases e publicação de runtime.
- `recuperacao.sh`: rollback transacional e tratamento de falhas.
- `reversao.sh`: pedidos explícitos de reverter ou reaplicar.
- `orquestracao.sh`: sequência transacional principal.
- `finalizacao.sh`: saúde final, tempos, card final e log técnico.

`atualizar.sh` executa uma cópia runtime estável e exporta
`TTS_BOT_UPDATER_SOURCE_DIR`. Assim uma atualização do próprio updater nunca mistura
módulos de duas revisões na mesma execução.

### `discord/`

- `constantes.py`: emojis, limites e constantes da integração.
- `cartoes.py`: renderer dos cards, detalhes, estado visual e log técnico.
- `preparacao.py`: inspeção do ZIP, operações declarativas e criação do candidato.
- `progresso.py`: eventos internos, heartbeat e edição do card.
- `controles.py`: botões, detalhes, cancelamento, rollback e reaplicação.
- `eventos.py`: recepção de anexos e eventos do Discord.
- `integracao.py`: composição dos mixins e inicialização do updater no bot.

`bot.py` mantém somente a composição com `IntegracaoDiscordUpdaterMixin` e os hooks
gerais do bot.

### `utilitarios/`

- `seguranca.py`: inspeção do ZIP, manifesto e integridade do candidato.
- `estado_git.py`: snapshots Git usados pelo core.
- `verificacao_runtime.py`: smoke check isolado do runtime candidato.
- `selecao_testes.py`: seleção incremental de testes do dashboard.

Os nomes antigos `snapshot_git.py` e `smoke_runtime.py` foram eliminados na Wave 46;
os caminhos canônicos são os nomes em português acima.

### `sistema/` e `sudoers/`

`updater/sistema/` contém `instalar.sh` e as units `bot-updater.*`.
`updater/sudoers/` contém a permissão mínima usada para disparar o serviço.

A Wave 47a instala a família `bot-updater.*`, migra o `OnFailure` do bot e
transfere os estados de timer/path independentemente. Os arquivos das units
antigas permanecem até a Wave 47b. O serviço que executa a migração nunca é parado;
o lock `/run/lock/tts-bot-updater.lock` continua compartilhado entre as duas famílias.
Falhas na instalação restauram os arquivos e os gatilhos anteriores.

### `testes/`

Todos os testes exclusivos do updater ficam em `updater/testes/`. Os nomes dos arquivos
são em português e tecnologias/protocolos preservam seus nomes oficiais quando isso
melhora a leitura (`Python`, `Node`, `TypeScript`, `Discord`).

Executar somente a suíte do updater:

```bash
python -m pytest -q updater/testes
```

Os helpers `fonte_core.py` e `fonte_discord.py` fornecem as visões expandidas usadas
pelos testes que precisam inspecionar a implementação modularizada sem duplicar código
no runtime.

## Segurança e transação

O candidato é preparado em worktree isolado. Operações `delete`, `move` e `rename`
são declaradas no `update-manifest.json`; exclusões já ausentes são idempotentes e não
são stageadas uma segunda vez. A árvore principal só é promovida depois das validações
do candidato.

Falhas após promoção acionam rollback e preservam evidências. Estados de recovery são
persistidos para que reinícios do bot ou do serviço não transformem uma falha conhecida
em um novo update.

## Dispatch

O caminho primário é orientado a evento:

`Discord -> queue/pending -> bot-updater.path -> bot-updater.service`

`bot-updater.timer` permanece como fallback. O bot também mantém o mecanismo de
dispatch/reconciliação para compatibilidade e observabilidade, mas não depende do timer
de um minuto para o caminho normal.

## Compatibilidade

As fachadas antigas em `scripts/`, `utility/`, `deploy/systemd/` e `deploy/sudoers.d/`
foram removidas nas Waves 45a/45b. O updater ainda reconhece nomes legados quando
necessário para interpretar candidatos antigos, mas não mantém implementações duplicadas
no repositório.
