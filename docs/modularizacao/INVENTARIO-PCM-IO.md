# PCM — IO, processos e preparos concorrentes

Entrada: rodada 26, commit `9169fd40471ebc4832c7c218bb9c567dc5c09b4b`.
Antes de editar: 593 arquivos/modos, 638 hashes, quatro JARs por tamanho/SHA/CRC,
gates completos da fonte/reconstrução e árvore limpa conferidos. Skill
core-worker-retomar indisponível; usados os documentos equivalentes do pacote.

| Fronteira na entrada | Dono/contrato | Lacuna |
| --- | --- | --- |
| Registry de streams | Mapa/RLock, ID aleatório e TTL no facade; lookup retorna cópia | Conclusão tardia de preparo após expiração/substituição |
| Preparo integral | ffmpeg via subprocess.run, timeout, replace e metadados no facade | Mesmo ID compartilha temporário; falha de promoção; limpeza durante preparo |
| PCM ao vivo | Processo por resposta; stdout por blocos; kill/wait/close no finally | stderr PIPE não consumido pode bloquear processo antes do áudio |
| Arquivo preparado | Content-Length, headers e blocos; fallback live opt-in | Arquivo substituído após stat, erro de leitura após headers |
| Áudio | s16le, 48 kHz, estéreo, 16 bits; frame 20 ms/3840 bytes | Preservar bytes, tamanhos de leitura e primeiro bloco |

Os 13 testes PCM existentes usam processos/pipes controlados e arquivos reais,
mas não cobrem saturação de pipe nem preparos concorrentes. A fixture de falhas
passa a gravar no output efetivo recebido pelo comando; mantém asserções de
preservação do destino e acrescenta ausência de qualquer resíduo temporário.

Primeiro caracterizar e corrigir os bugs reproduzidos; depois extrair IO sem
transferir mapas/locks/TTL para o serviço. Mesma ordem de parâmetros ffmpeg,
prepared por padrão e fallback opt-in. Sem dependência nova na VPS de 1 GB.
Provider/ffmpeg em aparelho e latência real não são comprovados por fixtures locais.
