# CDs — correções v8.3 e instalação

O bot está ativo; o updater terminou a atualização anterior. O emoji de
Rolling Girl já apareceu, mas o ZIP anexado das 17:19:36 contém um vídeo de
reação (`OcZM_Szee44`). O relatório correto das 17:18:48 seleciona o oficial
`vnw8zURAxkU`, com 44.780.292 visualizações observadas. A auditoria das 17:19
usa os critérios anteriores à v8.2. A sessão `--watch` vista no Termux explica
como um processo antigo pode continuar usando módulos carregados antes de
atualizar o kit. A versão nova protege os escritores do mesmo catálogo.

## VPS

Envie `osaka-cd-vps-correcao-v8.3.zip` ao updater e aguarde a conclusão. O
pacote depende do código v8/v8.1 já instalado. Contém somente código, testes
e documentação; conserva os CDs, capas e amostras já publicados.

Correções:

- Rejeição explícita por letra, partitura, recursos ou configuração da voz
  não suspende a rota de todas as vozes. Erros de rede/autenticação continuam
  com a proteção existente. Avisos mostram categorias específicas.
- Para letras compostas de vogais simples, o bot verifica o número mínimo
  de ataques vocais antes de usar a fila. Palavras arbitrárias continuam
  sendo analisadas pelo fonemizador real do worker.
- `_cd` tem prazos nas operações de abertura/configuração/edição, agrupa
  comandos simultâneos e libera o estado após falha. Excluir uma mensagem
  anterior lentamente não perde o painel novo já enviado.
- Seleção e amostra reconhecem a interação antes de operações mais lentas.
  Falhas restauram os controles; o visual do painel foi preservado.
- O arquivador para de selecionar permanentemente o mesmo post antigo com
  `metadata_mismatch`. Referências já publicadas continuam disponíveis;
  erros temporários mantêm o backoff. Esse defeito foi reproduzido com
  SQLite real e corresponde às 26 mil tentativas nos logs.
- O diagnóstico diferencia arquivo ausente, hash divergente, JPEG inválido
  e falta de Pillow no Python usado para executar o próprio diagnóstico.

`/cantar a a` é insuficiente para sete dos dez trechos iniciais: World Is Mine
precisa de pelo menos 4 sílabas, PoPiPo 18, Mesmerizer 5, Ochame Kinou 4,
Override 7, Teto Territory 4 e Triple Baka 4. Esses limites respeitam as pausas
e os limites de duração/alcance de cada vogal do backend 1.11.41. A correção
não remove pausas, repete palavras ou troca a partitura para esconder o erro.
O botão de amostra fornece “lá” para as notas e atende esse alinhamento.

Depois da aplicação, teste abrir `_cd`, selecionar, ouvir uma amostra pronta
e salvar. Para testar o canto ao vivo, forneça letra suficiente para a melodia;
um teste curto deve informar a incompatibilidade sem desativar outras vozes.

Use o Python confirmado no serviço para o diagnóstico da VPS:

```bash
cd ~/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python scripts/diagnose-cd-assets.py rolling-girl
```

Para logs filtrados sem aspas:

```bash
sudo journalctl -u tts-bot.service --since=-15min --no-pager | grep -e cd_panel -e tts_agent -e ttv_singing -e tts_panel_error
```

## Termux

Antes de atualizar, encerre o importador antigo na sessão original com Ctrl+C.
Para listar os processos:

```bash
pgrep -af import-midi.py
```

Na captura o importador tem PID 4778. Se ainda for esse processo, encerre-o:

```bash
kill -INT 4778
```

Se o PID já mudou, use o PID informado por `pgrep`. Esse é o processo do
importador; não encerre o Phone Worker. Baixe
`osaka-cd-termux-ajuste-v8.3.zip` em Downloads e execute:

```bash
unzip -o "$HOME/storage/downloads/osaka-cd-termux-ajuste-v8.3.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v8"
python deploy/ttv-cd/import-midi.py --help
python deploy/ttv-cd/import-midi.py --force
```

O diretório mantém o nome `osaka-cd-termux-kit-v8`. O catálogo local em
`~/.local/share/osaka-ttv-cd/` é preservado. A amostra pronta é reutilizada
quando sua partitura canônica não mudou. Confira `importer_version: 8.3`,
`video_id: vnw8zURAxkU`, `complete: true`, thumbnail e sample ready.

Envie ao updater o ZIP de música indicado por essa execução em `processed[].zip`.
Não escolha simplesmente o ZIP mais recente enquanto houver um importador
antigo ativo. O pacote das 17:19:36 anexado é estruturalmente válido, mas sua
seleção é um vídeo de reação. O ZIP das 17:18:48 mostrado no relatório contém
a seleção oficial; seus bytes não foram anexados para conferência local.

A v8.3 não permite outro escritor no mesmo catálogo. Para executar `--force`,
encerre primeiro o `--watch`. Se atualizar os arquivos Python com um watcher
v8.3 ativo, ele sai com `importer_code_changed` e pede reinício. Um processo
antigo anterior à proteção precisa ser encerrado manualmente nesta instalação.
Reactions, reviews, análises e comentários são excluídos quando não solicitados.
A contração Rolling/Rollin continua aceita; aliases e relatório completo são
preservados conforme a v8.2. Pacotes passam a declarar a versão do produtor.

Depois de regenerar e aplicar o ZIP da música, volte à automação sem `--force`:

```bash
python deploy/ttv-cd/import-midi.py --watch
```

## Evidência e limites

Os testes executam alinhador e fonemizador reais do Phone Worker 1.11.41,
processos concorrentes reais, SQLite temporário, MIDI/JPEG/MP3 e FFmpeg. As
interações Discord usam métodos de produção com transporte simulado. Não há
sessão Discord/SDK de produção aqui, nova síntese física UTAU, publicação de
rede ou acesso direto à VPS. Os 35 arquivos fornecidos do Phone Worker foram
comparados ao original e permanecem iguais. O backend, voicebank e resampler
não foram alterados nem instalados.

O relato de falhas ao vivo inclui uma mensagem genérica e logs de música,
sem o traceback de canto daquele horário. As causas reproduzidas foram
corrigidas; a aplicação dos novos patches e o teste real do canto ainda
precisam ser conferidos no aparelho/VPS. Emoji existente também pode ser
reutilizado sem os bytes locais; por isso a captura do emoji isoladamente
não confirma capa e catálogo corretos na versão instalada.
