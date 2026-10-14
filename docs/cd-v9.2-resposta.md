# Correção de resposta do _cd — v9.2

Esta correção é para a VPS com v9.1 já instalada. O comando `_cd` reutilizava
um painel ativo editando a mensagem antiga, sem resposta visível ao novo comando.
Agora ele responde com um link **Abrir painel**, preservando o painel existente,
sua seleção, amostra e arquivos em cache. Painéis expirados recebem uma nova
instância; não são reativados.

Também registra `[cd_panel] stage=received`, `stage=reused` e `stage=new_sent`,
com identificadores e tempo de abertura. Falhas continuam com timeout e código
de erro, sem conteúdo de mensagens ou credenciais.

## Instalar

Aplique **osaka-cd-vps-correcao-v9.2.zip** pelo updater de código e aguarde a
conclusão. Esse ZIP contém código; novos CDs continuam publicados automaticamente
pela VPN. As configurações privadas, o catálogo runtime e o Phone Worker não
entram no pacote.

O Termux Kit v9.1 já instalado continua sendo usado. Não há alteração no kit
nesta correção. Para iniciá-lo:

```sh
cd ~/osaka-cd-termux-kit-v9.1
python deploy/ttv-cd/configure-cd.py --check
python deploy/ttv-cd/import-midi.py --watch
```

Depois da atualização, envie `_cd`, aguarde mais de cinco segundos e envie
`_cd` novamente. A segunda chamada deve responder com **Abrir painel**, apontando
para o painel existente. Se continuar sem resposta, os novos registros podem
ser consultados na VPS com:

```sh
sudo journalctl -u tts-bot.service --since=-5min --no-pager -n 100
```

## MIDI novo para teste

**Ghost Rule — DECO*27 feat. Hatsune Miku** não consta no catálogo informado.
O arranjo de piano e o download são publicados por aregan em
https://www.aregan.net/p/ghost-rule.html. O autor solicita crédito.

Baixe **baixar-ghost-rule.py** em Downloads e execute no Termux:

```sh
python ~/storage/downloads/baixar-ghost-rule.py
```

O baixador salva o MIDI e seu JSON de identificação em **Downloads/osaka-cd-midi**,
verifica o contêiner MIDI, recusa HTML ou arquivos truncados e usa renomeação
atômica para não entregar um arquivo parcial ao watch. Arquivos existentes não
são sobrescritos. O importador valida notas, ritmo e arranjo e publica pela VPN.

A partitura é uma adaptação para piano; não é uma transcrição vocal confirmada.
O binário original não foi baixado neste ambiente devido à restrição de rede;
sua duração, quantidade de notas e polifonia ainda não foram medidas aqui.

## Testes

Os testes executam métodos reais de produção com uma fronteira Discord simulada,
incluindo resposta ao novo comando, reutilização, expiração, cancelamento,
limites de espera e atualização de catálogo. A regressão de resposta visível
falhou antes da correção. A qualidade do MIDI e a integração Discord conectada
dependem da verificação nos seus dispositivos.
