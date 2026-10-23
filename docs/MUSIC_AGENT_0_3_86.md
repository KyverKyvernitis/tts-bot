# Music Agent 0.3.86 — primeiro áudio e medição

A voz permanece no Termux. Esta entrega reduz esperas de controle antes da
primeira música e oferece aquecimento local opcional do resolvedor. A seleção
da fonte, o bitrate, os buffers PCM, o volume, os efeitos e a mistura de TTS
continuam seguindo o pipeline da 0.3.85.

## Controller

A reação de carregamento do seletor passa a usar o mesmo início em background
limitado do comando `_play`. A validação completa de worker pode ser
reaproveitada dentro da operação, respeitando sua validade original, a guild,
o destino da sessão e a configuração atual. Não se amplia o TTL de saúde.

O modal compartilha os caminhos rápidos do comando: consulta do arquivo por
guild, link individual resolvido no agente e preparação antecipada de voz para
links. Spotify e demais provedores de metadados, coleções, seleção explícita,
leases e cancelamento conservam seus contratos. A aprendizagem de playlists
usa o journal durável antes do processamento posterior dos aliases.

## Aquecimento opcional

No ambiente do Music Agent no Termux:

```dotenv
MUSIC_AGENT_YTDLP_PREWARM_ENABLED=false
```

Esse é o padrão. Para um piloto, use `true` e reinicie o agente pelo
procedimento existente. Um único helper inicializa yt-dlp e a configuração
local durante o login, com orçamento de cinco segundos. Não há busca de
músicas, extração de mídia ou download durante esse aquecimento.

O primeiro play pode compartilhar a inicialização em andamento dentro do
prazo normal de resolução. Falhas mantêm o fallback CLI com o orçamento
restante. A tarefa de aquecimento não condiciona a API de saúde nem o login
Discord. Desative a flag para retornar à inicialização sob demanda.

Esse recurso atua sobre o primeiro uso do resolvedor após iniciar o agente.
Uma nova conexão de voz com resolvedor já quente é um cenário diferente e
deve ser medida separadamente.

## Comparação no aparelho

Guarde as linhas completas `[music-start]` do controller e do agente antes e
depois, usando o mesmo aparelho, rede, faixas e versões de dependências:

```bash
python3 scripts/benchmark-music-start.py antes.log --compare depois.log
```

Compare p50, p95 e resultados por cenário. Separe arquivo conhecido, link
novo, escolha textual e primeira faixa de playlist. Faça duas rodadas
alternadas, com cinquenta tentativas por cenário principal. Inclua
arquivamento ativo e duas calls na verificação de concorrência.

O instante técnico de início continua sendo o primeiro pacote de música
enviado com sucesso ao Discord. Silêncio sintético e TTS isolado não contam.
As durações usam relógios monotônicos locais; não subtraia timestamps de
máquinas diferentes nem some etapas que ocorrem em paralelo. O tempo até a
confirmação no controller inclui retorno de rede e é um limite superior, não
o instante exato ouvido no cliente. A espera humana na seleção fica separada
da latência técnica.

Somente ampliar o piloto quando o p50 do fluxo alterado diminuir, o p95 não
aumentar nas duas rodadas e não surgir falha funcional, perda de áudio ou
piora de continuidade. Resultados inconclusivos mantêm o aquecimento
desativado. A validação local não substitui esse comparativo nem a escuta real.

## Atualização e continuidade

Atualize controller e runtime pelo procedimento existente e preserve os
endereços, tokens e demais configurações instaladas. O release de runtime
continua limitado a 63 arquivos mais o manifest, compatível com o bootstrap
1.0.0. Os novos auxiliares do controller e os documentos ficam fora desse
release do telefone.

A prioridade da próxima parte obrigatória, a cota global de decoders e a
limpeza antes de liberar vagas preservam a correção da 0.3.85. O áudio
permanente continua no fórum atual. Os detalhes dos testes desta entrega
constam em [Validação 0.3.86](VALIDACAO_MUSIC_AGENT_0_3_86.md).

Em regressão no piloto, desative o aquecimento ou restaure controller e runtime
0.3.85 juntos, mantendo o mesmo executor e os dados instalados.
