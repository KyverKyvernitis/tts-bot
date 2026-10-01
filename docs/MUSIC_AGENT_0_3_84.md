# Music Agent 0.3.84 — voz no Termux e recuperação de BrokenPipe

O perfil atual mantém voz no Termux. A alternância automática VPS/Termux não
foi implementada. O processo principal na VPS continua orquestrando comandos,
painel e metadados. Os arquivos permanentes continuam no fórum atual e o
arquivamento mantém seus diretórios temporários.

## Retornar ao perfil Termux

Se o serviço experimental da VPS foi ativado, pare-o ANTES de iniciar voz no
telefone, para encerrar suas sessões e liberar RAM:

```bash
sudo bash deploy/music-agent-vps/return-to-termux.sh
```

O script atua somente sobre `music-agent-voice.service`, sem tocar no processo
principal do bot, nos bancos nem nos secrets. Também é possível usar diretamente
`sudo systemctl disable --now music-agent-voice.service` se esse serviço existir.

No ambiente do controller, mantenha:

```dotenv
MUSIC_AGENT_VOICE_EXECUTOR=termux
MUSIC_AGENT_DIRECT_API_ENABLED=false
```

Atualize o controller e o runtime do Phone Worker para esta versão e reinicie
os processos pelo procedimento existente. O agente deve anunciar 0.3.84.
Mantenha `PHONE_WORKER_HOST`, porta e token que já usava.

`termux` é o padrão mesmo se a primeira variável não existir. Nesse perfil,
flags antigas da API direta não são consultadas e vínculos antigos com a VPS
não são usados. O supervisor Termux restaura `MUSIC_AGENT_EXECUTOR_MODE=full`,
mesmo que a configuração antiga estivesse em `archive` após a experiência VPS.
Voz e arquivamento voltam a ser funções do agente do telefone. Não há fallback
automático de voz para a VPS.

O perfil anterior fica preservado para compatibilidade de código, mas exige
escolha explícita adicional `MUSIC_AGENT_VOICE_EXECUTOR=vps` para ser reativado.
Ele não é o perfil recomendado/ativado nesta entrega de retorno ao Termux.

## Correções de falha transitória

O print fornecido mostra `BrokenPipeError: [Errno 32] Broken pipe` ao tentar uma
playlist Spotify. Sem traceback ou logs do ambiente não é possível confirmar
se o pipe era HTTP, stdout ou outro processo, nem atribuir os travamentos à VPS.
A versão anterior usava voz na VPS somente se essa opção fosse ativada.

Foi corrigida uma falha concreta no tratamento do erro:

- O proxy pode devolver `ok=false` e `BrokenPipeError` dentro de HTTP 200. O
  controller agora trata esse resultado como falha transitória de entrega,
  assim como uma exceção de rede. Play/playlist são reenviados com o mesmo
  `command_id` e o vínculo ao telefone é preservado. A idempotência do agente
  evita adicionar a mesma faixa duas vezes se a resposta original se perdeu.
  Respostas HTTP de erro do agente não são reenviadas automaticamente, pois
  podem ocorrer após uma alteração parcial na fila.
- Socket keep-alive encerrado recebe uma tentativa em conexão nova. Esse
  caminho não executa auditoria pesada nem reinicia o agente de voz. Status
  também se recupera dessa falha de socket. A repetição tem orçamento limitado.
- Controles temporais, como pause/TTS, não são acumulados para execução tardia.
  Quando falham, a mensagem é legível, sem o erro Python cru.
- Um stdout fechado não transforma telemetria em falha de playback. O agente
  tenta registrar em stderr, mantendo a reprodução e os comandos.

Mantidas as melhorias de arquivo, streaming, cache, playlists e aprendizagem
das versões anteriores. Nenhum limite artificial de quantidade foi recolocado
na aprendizagem ou arquivamento. Buffers/concorrência continuam limitados.

## Validação e diagnóstico

Os testes reproduzem BrokenPipe serializado, socket encerrado, repetição com o
mesmo ID, retorno ao destino Termux com flags antigas VPS e stdout quebrado.
Detalhes em [validação 0.3.84](VALIDACAO_MUSIC_AGENT_0_3_84.md).

Se o problema persistir após aplicar o perfil Termux, o traceback do momento da
falha e eventos `audio_read_stall`, `buffer_underruns`, `voice_runtime_recovery`
e `command_error` do `music_agent.log` permitem diferenciar rede, decoder,
concorrência e transporte. Esta execução não teve acesso à call ou aos logs do
telefone/VPS; a fluidez real ainda precisa ser conferida no ambiente do usuário.
