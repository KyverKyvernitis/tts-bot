# Pesquisa de metadados dos CDs

`cd_metadata.search_metadata(title, artist, aliases=(), limit=25, ...)` retorna
`selected`, `audit` e `candidates`. O importador usa essa função em uma thread.
`limit` limita resultados por consulta; o relatório registra cada consulta e
deduplica vídeos encontrados em consultas diferentes. Até 16 consultas são feitas.

A busca reaproveita o `ProvedorYouTubeMixin` do player, acrescentando estatísticas
de visualização e a ordenação por visualizações. A chave vem de
`YOUTUBE_API_KEY`/`GOOGLE_YOUTUBE_API_KEY`, sem entrar no relatório. Sem chave, usa
o mesmo extrator público `yt-dlp`; não lê cookies ou configurações de contas e
não baixa áudio. Estatísticas ausentes em candidatos plausíveis são resolvidas
individualmente. Falhas expõem apenas o nome da classe do erro.

O matching reaproveita normalização e atributos de versão do player; títulos
japoneses/CJK são preservados adicionalmente. Exige evidência explícita de título
e artista no título/canal. Covers, remixes, shows, tributos e outras versões não
solicitadas ficam rejeitados com motivo registrado. Vídeos de letra e áudio
oficial continuam candidatos à mesma gravação. A seleção informa sempre
`highest-view-count-among-inspected-identified-recordings` e nunca garante o
máximo global do YouTube. Contagens offline preservam sua data informada;
sem data, a observação permanece desconhecida. Contagens buscadas ao vivo têm
`view_count_observed_at`.

Aliases e artista precisam vir do nome ou sidecar do MIDI; traduções e
romanizações não são adivinhadas. Por exemplo, Rolling Girl precisa de aliases
`Rollin Girl` e `ローリンガール` para alcançar o upload oficial de wowaka. O vídeo
`xPhPHO-sKlY` é um tributo de Magical Mirai 2019; o oficial é `vnw8zURAxkU`.
Fontes públicas: [oficial](https://www.youtube.com/watch?v=vnw8zURAxkU) e
[tributo](https://youtu.be/xPhPHO-sKlY), verificadas em 2026-10-13.
O snapshot indexado encontrado nessa data mostrava 44.107.888 visualizações no
oficial; isso não representa um contador atualizado pela API ao vivo.

`duration` é opcional e deve representar a duração esperada da música completa,
quando confirmada. Um MIDI de trecho ou arranjo reduzido não fornece essa duração.
Não usar os 13,413 segundos da adaptação de teste para filtrar vídeos completos.

Para o Termux Kit, copie os seis caminhos em `PLAYER_SOURCES` a partir de
`cogs/musica/` para `player_metadata/`, preservando a estrutura relativa. São
módulos puros e inalterados. O loader usa namespaces privados, sem importar
configuração, Discord, router ou inicialização do bot. O fallback precisa de
`yt-dlp`; a busca pela API usa somente a biblioteca padrão de Python.

Validação isolada: `python -m unittest discover -s tests -p test_cd_metadata.py`.
