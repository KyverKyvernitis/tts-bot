# patch-startup-lag-music-restore-idempotent-20260603

Escopo: reduzir lag no startup causado pelo restore de música e impedir edições desnecessárias de canal no restart.

## Alterações

- Move o restore de bitrate/status de música para uma task pós-ready em background.
- Adiciona atraso inicial antes do restore de música, para o gateway estabilizar primeiro.
- Adiciona pequeno intervalo entre restore de bitrate e restore de status.
- Adiciona pequeno intervalo entre guilds durante reconciliação de startup.
- O restore de bitrate só edita canal se ele ainda estiver exatamente no bitrate temporário marcado pelo bot.
- Se o bitrate já está normal, foi alterado por staff, canal sumiu ou falta permissão, a marcação persistida é limpa sem insistir em todo restart.
- O restore de status do canal só chama o endpoint se ainda houver alteração real a fazer.
- Se o status atual já é o desejado, ou se staff alterou manualmente, a marcação persistida é limpa sem editar.
- Logs de restore de status ficam menos barulhentos: só loga `INFO` quando realmente restaurou algo ou quando um batch de pendências foi resolvido.

## Não alterado

- Updater/ZIP update/GitHub/rollback/redo não foram tocados.
- CallKeeper não foi tocado.
- Core Linux/rootfs/Bedrock não foram tocados.
- TTS runtime não foi tocado.
- Player/runtime de música não foi alterado, só o restore pós-startup de status/bitrate.
