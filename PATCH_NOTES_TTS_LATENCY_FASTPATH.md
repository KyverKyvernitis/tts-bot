# Patch TTS latency fast-path

Objetivo: reduzir latência real do TTS sem depender do worker quando ele está offline/lento.

Alterações principais:
- Health leve do TTS Agent em `/tts-agent/health`, sem coletar o `/health` completo do phone-worker.
- A VPS reutiliza `aiohttp.ClientSession` com keep-alive para chamadas ao worker.
- Health com tolerância: um timeout isolado não derruba uma rota worker que acabou de sintetizar com sucesso.
- Roteamento adaptativo: worker só é usado quando está online/pronto e faz sentido; gTTS curto usa fast-path VPS.
- Endpoint raw `/tts-agent/synthesize.raw` para devolver áudio binário, evitando JSON gigante + base64 no caminho do TTS Agent.
- Worker cacheia o client do Google Cloud TTS para evitar recriar credenciais/client a cada síntese.
- Painel `/vps` mostra `VPS fast-path` quando o worker está pronto, mas a rota adaptativa escolheu VPS por latência.
- `cog_unload` fecha a sessão HTTP persistente do worker.

Observação: não promete 10x em toda fala. O ganho grande vem em cache hit, worker online estável, Android/Google raw e quando a VPS deixa de esperar worker offline.
