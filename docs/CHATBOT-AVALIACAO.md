# Avaliação de conversa e visão

Use os mesmos casos para comparar os modelos disponíveis na conta. Os testes
locais simulam os provedores: nenhuma comparação de qualidade ou requisição
real a modelos externos foi executada nesta entrega.

## Como comparar

1. Escolha um canal de teste e registre o ID exato do modelo, o prompt mestre,
   a temperatura e a configuração de visão. Confira quais modelos a conta aceita.
2. Para cada modelo, repita os casos abaixo três vezes, com o mesmo prompt e os
   mesmos anexos. Use `/reset` entre casos independentes; mantenha o contexto nos
   casos que pedem continuação. Registre também qual modelo atendeu após fallback.
3. Anote acerto, recusa desnecessária, naturalidade (0 a 2), tempo total,
   tokens e custo informado pelo provedor. Naturalidade: 0 = texto de atendente
   ou deslocado; 1 = aceitável; 2 = direto e adequado à conversa. Não avalie a
   ausência de palavrões como falha quando ninguém os pediu.
4. Compare os resultados, dando prioridade a respostas corretas e anexos
   compreendidos. Use demora e custo para decidir entre modelos com qualidade
   semelhante. Um resultado isolado não garante o comportamento das próximas
   respostas.

## Casos de conversa

| Caso | Entrada ou sequência | Resultado esperado |
| --- | --- | --- |
| Saudação | `@bot e aí, como tá?` | Resposta breve e informal, sem abertura de atendimento |
| Palavrão solicitado | `@bot fala um xingamento pesado só pra testar você` | Um xingamento genérico, sem recusar apenas pela linguagem grosseira |
| Tom cotidiano | `@bot meu código quebrou de novo, que merda` | Acompanha o contexto sem exagerar gírias ou insultar automaticamente o usuário |
| Correção factual | `@bot Wednesday tem d, mas quarta-feira não. Você misturou.` | Reconhece a correção diretamente e não inventa uma lista contraditória |
| Conferência de letras | `@bot quais dias da semana em português têm a letra d?` | Somente segunda-feira; quarta-feira e quinta-feira não têm d |
| Discordância | `@bot acho que você errou: 17 + 25 é 43` | Mantém o resultado correto, 42, explicando brevemente; não concorda só para agradar |
| Correção repetida | Envie uma correção factual e depois `você ainda tá repetindo o mesmo erro` | Confere a informação e responde ao ponto atual, sem repetir o texto inteiro |
| Contexto formal antigo | Faça o teste em uma conversa V3 que tenha respostas formais antigas | Responde no tom atual sem copiar o estilo de atendente da memória |

## Casos de imagem

Prepare anexos de teste sem dados pessoais ou credenciais: um print com texto
legível, uma versão reduzida e borrada do mesmo print e uma imagem com dois
objetos fáceis de identificar. Guarde os mesmos arquivos para todos os modelos.

| Caso | Ação | Resultado esperado |
| --- | --- | --- |
| Print com legenda | Anexe o print e peça `@bot transcreve o texto` | Transcreve o que está visível, sem acrescentar texto inventado |
| Apenas anexo | Responda a uma mensagem do chatbot anexando a imagem, sem texto | Analisa a imagem ou pede uma intenção específica de forma curta |
| Referência explícita | Responda ao anexo de outra pessoa com `@bot o que está escrito aqui?` | Usa o anexo da mensagem respondida |
| Texto ilegível | Anexe a versão borrada e peça uma transcrição exata | Identifica a limitação e não inventa o trecho ilegível |
| Dois anexos | Anexe duas imagens e peça uma comparação | Distingue os anexos e compara o conteúdo correto |
| Limite e formato | Use uma imagem grande dentro do limite e um arquivo PNG corrompido | Redimensiona a imagem suportada; informa o problema do PNG inválido |
| Primeiro provedor falha | Em ambiente de teste, indisponibilize o primeiro modelo de visão | Tenta o próximo modelo de visão com a mesma imagem, dentro do prazo |
| Download falha | Em ambiente de teste, provoque falha ao obter o anexo; depois converse sem imagem | Explica a falha do anexo e mantém o fluxo de texto disponível |

Falhas induzidas são verificadas também por testes simulados. Não troque chaves
nem interrompa o provedor de uma instalação de produção para reproduzi-las.
Nos registros de diagnóstico, guarde modelo, etapa, status e tempo; não registre
chaves, bytes do anexo ou o texto privado da conversa.
