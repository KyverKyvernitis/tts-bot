# Core Worker para PC

Este diretório traz o primeiro runtime de **Core Worker universal** para PC Linux. Ele reaproveita o mesmo `phone_worker.py` e o mesmo `music_agent.py`, mas roda fora do Termux e se apresenta ao registry como `linux_pc`.

O fluxo esperado é:

1. No Discord, abra o painel `workers` e use **Parear worker**.
2. No PC, instale o worker com `install-linux.sh`.
3. Rode `pair-core-worker.sh CORE-XXXX https://sua-vps:10000 NomeDoPC turbo`.
4. Ative o serviço de usuário `core-worker.service`.

O PC worker usa conexão de saída para a VPS: heartbeat, busca jobs e envia resultado. Para jobs normais não precisa abrir porta no roteador. Para música/voz, ele precisa do token do bot no `.core-worker.env`, ffmpeg e dependências de voz do discord.py.

## Instalação Linux rápida

```bash
cd deploy/pc-worker
bash install-linux.sh
cp core-worker.env.example ~/.core-worker.env
nano ~/.core-worker.env
bash ~/core-worker/pair-core-worker.sh CORE-XXXX https://sua-vps:10000 Meu-PC turbo
systemctl --user enable --now core-worker.service
```

## O que este worker anuncia

- `device_type=linux_pc`
- `source=linux-pc-worker`
- capacidades de diagnóstico, ffmpeg/ffprobe, validação de ZIP, TTS e música direta via yt-dlp/FFmpeg quando o perfil for `turbo`.

Lavalink/NodeLink não fazem parte deste runtime.
