# Correção do painel e da automação de CDs — v8.1

O painel mantém o visual solicitado: todas as notas e oitavas, quantidade de
notas, duração total, capa, seletor e os dois botões. A duração de cada nota não
aparece. Os avisos privados de expiração, autorização e erro reutilizam os de
`_tts`; a expiração indica o comando `_cd` e respeita o prefixo do servidor.

## Causas reproduzidas e correções

- A primeira rejeição ao criar um emoji interrompia a sincronização dos CDs
  seguintes. Cada falha específica agora recebe recuperação independente.
- A recuperação dependia de outro acesso ao painel. O serviço acompanha o
  catálogo e agenda novas tentativas mesmo sem um painel aberto.
- Um emoji criado enquanto a mensagem inicial era enviada podia ficar fora
  dos controles já serializados. O painel confere a geração do registro depois
  do envio e atualiza os controles quando necessário.
- Trocar ou desligar o cog durante uma sincronização não pode iniciar outra
  tarefa antiga nem registrar uma resposta de outro bot.
- Uma falha temporária de leitura não fica gravada indefinidamente no cache.

Essas falhas foram reproduzidas com código de produção e transporte Discord
simulado. A rejeição específica da VPS ainda exige as linhas `[cd_emoji]`.
O painel pode mostrar a URL pública da thumbnail mesmo sem ter o JPEG local
aprovado pelo hash; somente esse arquivo local serve para criar o emoji.

## Reutilização e velocidade

O registro reaproveita os emojis da aplicação por nome determinístico e hash
da capa. Erros de limite, permissão e rate limit respeitam espera global. Uma
resposta perdida após criação força consulta antes de nova tentativa, evitando
duplicatas. Não há exclusão automática de emojis.

JPEGs e MP3s aprovados têm cache compartilhado limitado a 8 MiB e 512 entradas.
Cada uso confere assinatura do arquivo e hash declarado. A alteração da capa
invalida seu emoji e cor, preservando a amostra; a alteração da partitura
invalida o áudio. Consultas de nomes dos emojis não decodificam todas as capas.
Arquivos removidos ou substituídos deixam de usar bytes antigos.

Reabrir `_cd` no mesmo canal reutiliza o painel pessoal ainda válido. Edições
com a mesma capa reutilizam o anexo existente. Consultas de preferências na
navegação têm cache curto; abrir, ouvir e confirmar forçam leitura atual.
Uma amostra instalada válida dispensa consulta ao worker. Atualizações em
segundo plano nunca renovam uma interação expirada.

## Instalação e diagnóstico

Envie `osaka-cd-painel-automacao-vps-v8.1.zip` ao updater, como os patches
anteriores. Ele depende do código v8 já instalado. O pacote não contém
catálogos de músicas, capas, áudio, configurações ou arquivos do Phone Worker.
Assim, a versão de Rolling Girl já aplicada na VPS é preservada.

Depois de abrir `_cd` e selecionar Rolling Girl, obtenha os logs na VPS:

```bash
sudo journalctl --since "2 hours ago" --no-pager | grep -F '[cd_emoji]'
```

Da raiz do projeto, o diagnóstico local confere manifesto, JPEG, SHA-256 e a
conversão real para emoji, sem acessar o Discord nem mostrar credenciais:

```bash
python3 scripts/diagnose-cd-assets.py rolling-girl
```

Se o processo não registra no journal, o comando pode ficar vazio; nesse caso,
as linhas devem ser obtidas do destino de logs usado pelo serviço.

O ajuste separado do Termux inclui filtros de covers, versões, performances,
instrumentos e mudanças de andamento, além de resumo curto no terminal e
relatório JSON completo em arquivo. Para atualizar o kit v8 já instalado:

```bash
unzip -o ~/storage/downloads/osaka-cd-termux-ajuste-v8.1.zip -d "$HOME"
cd ~/osaka-cd-termux-kit-v8
python deploy/ttv-cd/import-midi.py --help
```

## Validação e limites

85 testes isolados passaram nesta entrega: 7 de cache, 9 de registro de emojis,
3 de avisos/disponibilidade, 6 de reutilização do painel, 4 do observador do
catálogo, 4 de capas/diagnóstico, 9 de catálogo/amostra, 21 de metadados, 2 de
relatório, 9 de importação, 7 de configuração do worker e 4 de recuperação.
Há JPEGs convertidos e MP3s decodificados com FFmpeg de fato. A recuperação
também verifica o MP3 fornecido de Rolling Girl, com duração de 13,513 segundos.
O áudio sintetizado nos testes genéricos confere formato/transporte, não a voz.

Os testes de interações executam métodos reais com transporte simulado; não
houve sessão Discord ativa nem execução da suíte dependente do SDK/pytest,
indisponíveis neste ambiente. A causa real da rejeição do emoji e a aplicação
desse novo patch na VPS não estão confirmadas. O usuário já confirmou a
aplicação do ZIP de Rolling Girl e a reprodução da amostra até cerca de 14 s.
Não houve nova síntese UTAU física nesta entrega. Os 35 arquivos fornecidos do
Phone Worker foram comparados ao original e permanecem iguais.
