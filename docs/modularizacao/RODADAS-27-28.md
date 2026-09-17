# Rodadas 27–28 — processos, preparação e IO PCM

Entrada: `9169fd40471ebc4832c7c218bb9c567dc5c09b4b` (rodada 26).
593 arquivos/modos, 638 hashes e quatro JARs íntegros antes das edições.
Inventário e lacunas: `INVENTARIO-PCM-IO.md`. Implementação em 14/09/2026.

## Rodada 27 — caracterização e correções

Entrada: 9 failed, 16 passed, zero mutações. Doze cenários novos mais os treze
existentes demonstraram sete problemas: stderr sem consumidor bloqueando áudio;
mesmo stream disparando transcoders com temporário compartilhado; limpeza apagando
temporário ativo; conclusão publicando após expiração/substituição; temporário
abandonado após falha de promoção; Content-Length e bytes de arquivos diferentes;
segunda resposta HTTP/fallback depois de headers e erro de leitura.

Código corrigido ainda no facade: stderr do processo live vai para DEVNULL,
pois antes não era consumido nem exibido. Sem thread de drenagem ou buffer novo.
Preparos do mesmo ID usam exclusão por stream e reutilizam o resultado publicado;
IDs distintos continuam concorrentes. Mapa de preparos e registry/RLock ficam no
facade; referências são liberadas no finally após sucesso/falha, evitando retenção
dos locks inativos. O item válido de cache continua sendo retornado por identidade.

Cada preparo usa diretório temporário próprio, fora do glob de limpeza *.pcm,
com remoção em toda saída. Publicação verifica sob o mesmo RLock que o registro
original ainda é o dono e não expirou; replace e atualização de metadados ficam
na mesma seção. Arquivo/registro de uma geração substituta não recebe efeitos
tardios. Timeout do subprocess.run e mensagens de falha permanecem.

Arquivo preparado é aberto antes dos headers; tamanho e bytes vêm do mesmo
descritor. Falha após iniciar a resposta fecha a conexão sem iniciar live ou
enviar um segundo status. Falha anterior aos headers mantém o fallback opt-in.
Codec s16le/48 kHz/estéreo/16 bits, frame 20 ms e blocos de leitura preservados.

Na primeira correção, os nove cenários vermelhos passaram, mas um teste existente
detectou perda da identidade do item em cache: 1 failed, 24 passed. Corrigida a
seleção do item sem relaxar o teste. Acrescentados timeout real com retry/limpeza,
fallback antes dos headers e comando ffmpeg integral. Seleção integrada final:
116 passed, zero mutações; são 16 casos de IO PCM, 13 de ciclo PCM e as regressões
de Voice State/probes, fronteiras Python e streaming TTS. Não somar as seleções.

Os processos são Python local com pipes reais, encerrados/recolhidos nas fixtures;
não há ffmpeg remoto, rede real ou jobs enviados. Java, bot/VPS, TTS de fala,
Voice State/probes, Music Agent separado e self-builder não mudaram.

Pendências: subprocess.run do preparo continua coletando stderr em memória;
expiração invalida publicação, mas não interrompe proativamente o transcoder já
em curso. Limpeza de arquivos publicados continua por melhor esforço. Abertura
única protege troca por replace/unlink, não reescrita externa in-place. Não medir
esses cenários como latência real de ffmpeg/aparelho ou validação de plataforma.

Próximo passo concreto: extrair builder de comando, transcodificação, envio de
arquivo preparado e stream live para serviço opcional sem estado. Manter
registry, coordenação por stream e validação/publicação do dono no facade;
incluir distribuição/hash, import sem efeitos, preload e chegada tardia.

## Rodada 28 — extração de IO PCM

`pcm_io.py` tem quatro funções/189 linhas: builder de comando, transcodificação,
resposta de arquivo preparado e stream live. Recebe callbacks/IO/clock na chamada;
registry, coordenação por stream, RLock e validação/publicação do dono ficam no
facade. As quatro funções são equivalentes por AST às versões corrigidas da
rodada 27 após substituir bindings e extrair o callback de publicação. A coordenação
corrigida e o callback mantêm exatamente a mesma AST. WorkerHandler não mudou.

Loader opcional valida as quatro funções, carrega uma única instância sob
concorrência, permite retry após ausência/incompletude/sintaxe inválida e possui
preload independente antes de jobs/HTTP. Import não inicia IO, processo, rede ou
thread. A falta do módulo no modo live devolve um único erro antes de iniciar
processo; recuperação mínima de controle permanece disponível.

Distribuição/hash incluem pcm_io.py 0644. Release local de 34 membros incluindo
manifesto, aceita pelo extrator original 1.0.0. São 27 testes de release e 33 de
serviços opcionais; os testes de serviços foram generalizados para PCM mantendo
os três serviços TTS anteriores. Import da release extraída usa site-packages
desativado. Nenhum ZIP de release foi publicado.

Seleção após extração: 176 passes, zero mutações. Três testes adicionais aprovados
verificam bindings após aquecer o módulo (diretório, IO, clock, registry/RLock,
publicação e tamanho dos blocos/fstat) e módulo ausente no stream live. Total do
arquivo novo de IO PCM: 19. Comando ffmpeg integral, byte stream, fallback opt-in,
identidade de cache e limpeza dos processos mantêm regressões ativas.

Fechamento previsto: fonte congelada, suíte geral com Java obrigatório, lint e
sintaxe completa; diff aplicado à extração limpa da rodada 26, comparação dos
597 arquivos/modos e suíte geral na reconstrução. Resultados finais e hashes
ficam no registro externo 28, sem editar a fonte após os gates. Inventário atual:
96 atribuições globais, 324 funções e 10.983 linhas no facade; quatro JARs íntegros.
A auditoria compara 585 arquivos da entrada fora do escopo, incluindo os 44
protegidos anteriores. TTS de fala e Voice/probes têm corpos idênticos à entrada.

Pendências: permanecem os limites PCM registrados na rodada 27; Music Agent/ciclo
async, self-builder Python, UI, discovery, fonte Coptic e gates Java/plataforma
continuam em aberto. Sem ffmpeg/providers reais, latência em aparelho, Gradle/APK,
publicação, instalação ou jobs remotos. VPS de 1 GB sem worker obrigatório.

Próximo passo concreto: inventariar e caracterizar lifecycle async do Music Agent,
especialmente stop/disconnect/cancelamento e efeitos tardios; depois separar a
unidade sustentada por esses testes. Preservar fila, áudio, mapas/locks e bindings.
Checkpoint completo + diff permanece intermediário.
