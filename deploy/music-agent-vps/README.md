# Voz no Termux — perfil 0.3.85

A voz permanece no Termux. A VPS atende o controller, os comandos e o painel.
O Phone Worker atende reprodução, resolução e arquivamento. Os arquivos de
áudio permanentes continuam no fórum atual, com staging em diretórios
temporários durante o arquivamento.

No controller, use:

```dotenv
MUSIC_AGENT_VOICE_EXECUTOR=termux
MUSIC_AGENT_DIRECT_API_ENABLED=false
```

Esse é o padrão mesmo se a primeira variável não existir. Flags antigas da API
direta não movem a voz para a VPS. O supervisor do Termux restaura o modo
`full`, incluindo quando o ambiente anterior estava configurado em `archive`.
Atualize controller e runtime do telefone juntos e reinicie os processos pelo
procedimento existente; preserve os tokens e o endereço do Phone Worker.

Caso tenha ativado o serviço opcional da versão 0.3.83, pare-o na VPS antes de
iniciar voz no telefone:

```bash
sudo bash deploy/music-agent-vps/return-to-termux.sh
```

O script para e desabilita somente `music-agent-voice.service`, quando esse
serviço existe. Não altera o processo principal do bot nem os secrets. Os
arquivos do perfil experimental foram preservados para compatibilidade, mas
não são ativados nesta entrega. Não há alternância automática entre executores.

Veja [as instruções de retorno e recuperação de BrokenPipe](../../docs/MUSIC_AGENT_0_3_84.md)
e [a correção de continuidade no Termux](../../docs/MUSIC_AGENT_0_3_85.md).

Para medir o início da reprodução, salve os eventos `[music-start]` de antes e
depois e rode:

```bash
python3 scripts/benchmark-music-start.py antes.log --compare depois.log
```

O relatório calcula mediana e p95 de durações locais até o primeiro pacote
musical enviado. O som recebido pelo cliente Discord ainda depende da rede e
do cliente; esta execução não mediu uma call no ambiente de produção.
