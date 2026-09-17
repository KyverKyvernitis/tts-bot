# Rodadas 14–16 — plano de controle do Phone Worker

Continuação de `e86e4d2047d5a121c6754c115a6a99e3995d6bf8`, em 10/09/2026.
O inventário de entrada conferiu os 565 arquivos e os 281 hashes do pacote da
rodada 13 antes de qualquer edição. Java e isolamento já estavam consolidados;
não são apresentados como trabalho novo desta continuação.

## Rodada 14 — caracterização e correções

Código: autenticação continua lendo aliases e ambiente na chamada. A configuração
agora rejeita URL que se torna vazia após normalização. Endpoints IPv6 recebem
colchetes. O payload copia roles/capabilities antes de acrescentar capacidades e
trata exceções e respostas não objeto de probes opcionais. No pareamento, o ID
físico acompanha o ID escolhido; roles/capabilities solicitadas são normalizadas
sem mudar o ambiente antes de obter confirmação do servidor.

Testes executados: entrada com 7 falhas e 22 passes; teste adicional de pareamento
rejeitado falhou antes da correção. Depois das correções, 68 passes nos testes de
plano de controle, configuração, auto-enrollment e limites Python. Uma rodada
adicional de 32 passes inclui persistência após pareamento bem-sucedido e fixture
JSON integral capturada do monólito corrigido, antes da extração. Nenhuma mutação
de runtime/release foi registrada. Env, probes e respostas HTTP são locais aos
testes; nenhum job foi enviado a um worker.

Pendências: a montagem ainda está no facade; ainda não houve gate Java/completo
nesta continuação. A fixture fixa o contrato normal, não mede latência real.

Próximo passo concreto: extrair a montagem do payload para um módulo puro e
provar recuperação com entrypoint isolado, chegada tardia e registry local.

## Rodada 15 — payload puro e recuperação

Código: `phone_worker_runtime/control_plane.py` compõe o payload normal a partir
de dados explícitos. O facade mantém o envelope mínimo de recuperação, aliases,
política de safe mode, snapshots de jobs/rede e os donos de estado/locks. A porta
efetiva é capturada uma vez por payload. Import e carga são lazy; ausência, erro
de sintaxe ou módulo incompleto não ficam em cache. O heartbeat mínimo omite
telemetria opcional e informa `health.payload_mode=bootstrap`; o registry preserva
as medidas anteriores. A chegada do módulo restabelece o payload completo.

Testes executados: 32 passes imediatamente após extrair, incluindo igualdade com
o JSON anterior. Na ampliação houve 1 falha no teste que esperava timestamp de
rede bruto; o contrato existente retorna idade e o teste foi corrigido. O gate
completo da rodada passou 73 testes: chegada tardia, import sem writes/processos/
rede/threads, instância única, estado reatribuído, registry local, auto-enrollment,
limites Python e release. O arquivo novo entra nas duas allowlists com modo 0644;
27 arquivos mais manifesto são aceitos pelo extrator original 1.0.0. Edição só no
módulo muda o hash e a release anterior permanece intacta.

Pendências: transporte HTTP ainda no facade; gate geral/Java desta continuação
será executado após revisão dos recursos de erro HTTP. A recuperação mínima não
equivale a instalação completa. O fluxo normal conserva a fixture integral.

Próximo passo concreto: testar limites, fechamento de respostas e erros de leitura
nos clientes JSON/download sem chamadas remotas, corrigir falhas demonstradas e
fechar os gates gerais e a reconstrução pelo patch.

## Rodada 16 — fechamento de recursos HTTP e checkpoint

Código: os quatro clientes JSON e o downloader fecham também respostas HTTPError,
inclusive se a leitura falhar. Uma leitura interrompida do corpo de erro agora
registra falha de rede nos clientes remotos; clientes locais continuam sem alterar
essa telemetria. O downloader remove seu temporário nessas falhas e preserva o
arquivo de destino anterior. Métodos, headers, JSON, timeouts, limites e erros
retornados permanecem caracterizados. Nenhum probe, retry ou processo novo entrou
no caminho TTS; a classe WorkerHandler e o transporte dedicado não foram movidos.

Testes executados antes do fechamento: 10 falhas e 31 passes reproduziram o não
fechamento dos corpos de erro. Depois da correção, 97 passes em HTTP, plano de
controle e distribuição, sem mutações de runtime/release. Os testes HTTP usam
corpos em memória; os testes de registry e download usam diretórios temporários.

Gates de entrega: suíte geral com Java obrigatório, lint/sintaxe, comparação AST e
hash das áreas preservadas, aplicação do diff sobre cópia limpa da rodada 13,
comparação byte/modo e testes dessa reconstrução. Seus resultados consolidados e
os hashes exatos ficam no registro 23 e em `evidencias/rodadas-14-16/` do pacote
externo, escritos após os gates para não alterar a fotografia testada. Este texto
registra os testes intermediários efetivamente executados até seu fechamento.

Pendências: Voice Agent, TTS com estado estabilizado, Music, self-builder e UI;
discovery seguro e validações Java/plataforma discriminadas nos registros
anteriores. Não houve Gradle/APK, JNI real, instalação, publicação, job remoto ou
medição de latência em aparelho. A fonte Coptic ausente da base continua pendente.

Próximo passo concreto: inventariar streams, cancelamento/expiração e callbacks
do Voice Agent, caracterizar seu ciclo com testes locais e extrair a primeira
unidade sem copiar o mapa/lock do facade. Manter loops, outbox e timers do plano
de controle sob seu dono atual até migrar deliberadamente esses contratos.
