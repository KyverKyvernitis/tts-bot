# Osaka CD v9.3 — abrir um painel novo, como _tts

Cada nova chamada aceita de `_cd` prepara o painel, encerra e apaga o anterior
do mesmo usuário/servidor e envia um painel novo no canal atual. A resposta
**Abrir painel** e a edição do painel antigo ao reabrir o comando foram removidas.

A aparência permanece a mesma: notas, oitavas, duração total, capa, seletor e
botões. Os caches de capas, emojis e áudio continuam compartilhados; abrir uma
mensagem nova não faz pesquisa de vídeo nem repete síntese.

O cooldown continua com cinco segundos, como nos outros painéis. Uma abertura
concorrente não inicia outra criação para o mesmo usuário. Painéis anteriores
são marcados como expirados antes da tentativa de exclusão, inclusive se o
Discord recusar a exclusão. Limites de espera e recuperação permanecem ativos.

## Instalar

Aplique **osaka-cd-vps-correcao-v9.3.zip** pelo updater de código e aguarde sua
conclusão. O pacote substitui a correção v9.2 e pode ser aplicado sobre a v9.1
ou v9.2 já instalada. Não contém configurações privadas, dados do catálogo nem
arquivos do Phone Worker.

O Termux Kit v9.1 continua instalado e pareado. Esta alteração é somente na VPS.
O importador pode continuar rodando. Caso ele esteja parado, inicie no Termux:

```sh
cd ~/osaka-cd-termux-kit-v9.1
python deploy/ttv-cd/import-midi.py --watch
```

## Conferir

Envie `_cd`, aguarde mais de cinco segundos e envie `_cd` novamente. O primeiro
painel deve desaparecer e um novo painel deve aparecer no fim da conversa.
Painéis de outros usuários permanecem disponíveis. O envio não inclui uma
mensagem separada com link.

Os testes isolados executam os métodos de produção com fronteira Discord
simulada e conferem a ordem de exclusão/envio, expiração, isolamento por usuário,
cancelamento, recuperação e fechamento dos anexos. A validação conectada depende
de conferir o comportamento no Discord após aplicar o pacote.
