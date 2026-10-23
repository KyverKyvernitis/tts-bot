# Instalação do patch incremental 0.3.86

Este ZIP contém somente os 25 arquivos adicionados/alterados na implementação
e este guia: **26 arquivos**, com o prefixo original `tts-bot-main/`.
Ele se destina à base 0.3.85 enviada nesta conversa. Não contém a base inteira.

## Aplicar

Use o updater de ZIP do bot, que preserva as configurações e prepara o rollback.
Se aplicar manualmente, faça backup e mescle o conteúdo de `tts-bot-main/`
na raiz já instalada, onde está `bot.py`. Evite criar uma segunda pasta
`tts-bot-main` dentro da raiz do projeto. Reinicie o controller na VPS pelo
procedimento habitual do serviço.

Os arquivos `*.env.example` são exemplos: não substitua os arquivos de ambiente,
tokens ou cookies já configurados. Não há nova dependência obrigatória nem
migração de banco neste patch.

## Depois de instalar

1. **Atualize também o runtime do telefone.** Pelo updater, a automação publica
   a release do worker; o bootstrap do Termux a consulta normalmente em até
   cinco minutos, promove a release e reinicia o processo. O telefone precisa
   estar online. Se necessário, use a ação existente de atualizar worker.
2. **Confira o Music Agent em execução:** a versão deve ser `0.3.86` e o agente
   deve estar pronto no Discord. Confira o status do serviço `music-agent` ou
   a última sessão de `~/phone-worker/music_agent.log`. A versão geral do Phone
   Worker é outra; ela não precisa passar a ser `0.3.86`.
3. **Teste primeiro com o aquecimento desativado**, que é o padrão mesmo se a
   variável nova não existir. As melhorias de seletor/modal já funcionam assim.
   Teste a primeira música da call por link arquivado, link novo, pesquisa com
   seleção e primeira faixa de playlist. Confira também seek, stop, transição
   de partes, efeitos e TTS durante a música.
4. **Só depois habilite o piloto do aquecimento**, se desejar. No ambiente real
   do Music Agent no Termux, normalmente
   `~/phone-worker/secrets/music-agent.env`, ajuste uma única ocorrência:

   ```dotenv
   MUSIC_AGENT_YTDLP_PREWARM_ENABLED=true
   ```

   Reinicie o serviço **`music-agent`** pelo controle de serviços do worker.
   Reiniciar apenas o controller na VPS não recarrega essa configuração.
   O log deve registrar `ytdlp_prewarm_finished` com `ready=True`; se falhar,
   consulte o erro e mantenha o fallback normal. O ganho desse aquecimento é
   avaliado no primeiro uso do resolvedor depois de reiniciar o agente.
5. **Compare os logs `[music-start]` antes/depois** com o mesmo aparelho e rede:

   ```bash
   python3 scripts/benchmark-music-start.py antes.log --compare depois.log
   ```

   Compare p50, p95, falhas e continuidade por cenário. Não amplie o piloto se
   houver piora de p95, falha nova, cortes ou perda de qualidade. Para desligar
   apenas o aquecimento, use `false` e reinicie o serviço `music-agent`.

## Se a atualização do telefone não chegar

Se os arquivos foram aplicados manualmente na VPS e a automação não publicou
a release, execute na raiz do projeto, no mesmo ambiente Python/configuração
do serviço:

```bash
python3 scripts/core-worker-automation.py after-update --force-agent
```

Depois de confirmar a publicação na VPS, pode antecipar a consulta no Termux:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check --force
```

Esse comando já atualiza e reinicia por padrão. Se o runtime foi promovido,
mas o worker ainda precisa de reinício manual, use:

```bash
bash ~/.core-worker-runtime/current/start-phone-worker.sh --force-restart
```

Esses caminhos correspondem à instalação canônica com bootstrap. Preserve
os caminhos personalizados, caso sua instalação os utilize. Um reinício sem
antes receber/publicar a release nova não instala o patch no telefone.

## Validação e retorno

As 86 verificações suplementares locais e a compatibilidade do pacote de
runtime foram verificadas. A suíte completa `pytest` e a medição em call real
continuam pendentes; não há porcentagem de ganho comprovada nem declaração de
ausência de regressões. Veja [o relatório](VALIDACAO_MUSIC_AGENT_0_3_86.md).

Se o problema persistir com o aquecimento desligado, restaure os arquivos do
controller pelo backup/rollback e distribua a release anterior ao telefone
pelo mesmo procedimento de atualização. Confira o retorno do Music Agent à
versão anterior, mantendo as configurações e os dados instalados.
