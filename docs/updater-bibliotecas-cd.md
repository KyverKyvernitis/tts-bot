# Correção pequena do updater: bibliotecas de CD

O updater concluía o ZIP v9.5, mas não marcava mudanças em `osaka_cd/` como
alterações do processo do bot. Os arquivos ficavam novos no disco, enquanto
o processo continuava usando a função da v9.4 já importada em memória.
Por isso uma mensagem curta ainda podia tocar todas as notas do trecho.

## Recuperar o bot agora

Na VPS, execute:

```sh
sudo systemctl restart tts-bot.service
```

Após o bot voltar, teste `'a ~` novamente com seu CD selecionado. Com a v9.5
já aplicada, o novo processo deve usar apenas duas notas. Não é necessário
reimportar os CDs ou apagar caches: a política v9.5 já separa essas gravações.

## Instalar a correção do updater

Envie **osaka-updater-rastreio-bibliotecas.zip** ao updater de código e
aguarde concluir. Ele altera apenas uma rotina de produção, acrescentando:

- `osaka_cd/*.py`, incluindo subpastas como `osaka_cd/phonetics/`;
- `updater/api_cd.py` e `updater/__init__.py`, importados pelo bot/webserver.

Essas mudanças passam a marcar `BOT_CHANGED=1`, provocando o reinício normal
e sua verificação de saúde. Mantém-se o limite de um reinício por fase do
updater. Elas não provocam uma nova sincronização de slash commands nem uma
atualização do Phone Worker.

O updater executa uma cópia estável de seus módulos durante cada atualização.
Por isso a nova regra vale a partir da próxima execução do updater. Este
primeiro ZIP corrige apenas o updater; o reinício manual acima carrega a
correção vocal v9.5 que já está instalada.

Mudanças apenas em documentação, testes ou imagens da biblioteca não exigem
reiniciar o bot. A publicação automática dos CDs continua usando a API e a
troca de catálogo em execução.

## Termux

Este ajuste é somente na VPS. Continue usando o kit já instalado e pareado:

```sh
cd "$HOME/osaka-cd-termux-kit-v9.1"
python deploy/ttv-cd/import-midi.py --watch
```

Se o `--watch` já está rodando, deixe essa sessão em execução; não abra outra.
Não há ZIP novo do kit para este ajuste do updater.
