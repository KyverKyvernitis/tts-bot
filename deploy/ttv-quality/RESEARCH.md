# Pronúncia e junções de fala no TTV

Esta revisão usa as banks já instaladas da Teto e da Miku e o ESPER configurado
no aparelho. O português continua adaptado: gravar ou encontrar um alias não
acrescenta à bank um fonema português ausente. Cobertura de aliases, volume e
geração válida de WAV não comprovam inteligibilidade ou naturalidade.

## Referências consultadas

- [Léo ao Léu — Tutorial UTAU: Usando português CVVC](https://leo-ao-leu.blogspot.com/2013/02/utau-tutorial-usando-portugues-cv-vc_3.html):
  explica a montagem de transições VV, VC, CV e CC, os crossfades, os ditongos,
  as vogais nasais e a redução de vogais finais no português brasileiro.
  O [arquivo no UTAUrials](https://utaurials.wordpress.com/2017/10/20/usando-cvvc-em-portugues/)
  é uma republicação autorizada do mesmo tutorial, e não uma segunda opinião
  independente.
- [Piapro Studio — A Small Pronunciation Guide](https://piaprostudio.com/?p=3411&lang=en):
  distingue texto e representação fonética e ressalta que palavras escritas
  igualmente podem ter pronúncias diferentes. A correção de um fonema também
  precisa respeitar sua conexão com os fonemas vizinhos.
- [VocaVerse — How to fix choppy transitions?](https://vocaverse.network/threads/how-to-fix-choppy-transitions.2343/):
  registra um caso de transição ruim da Miku que melhorou com ajuste do ataque.
  É uma experiência individual com Vocaloid V4, não um teste do ESPER.

As orientações da comunidade servem de referência para verificar nosso
planejamento. Parâmetros exclusivos do Vocaloid/Piapro não são comandos do
ESPER. Os textos e bancos dessas fontes não foram copiados para o renderer.

## Correções verificáveis

Um CV simples contém a consoante de entrada e a vogal, mas não substitui
necessariamente a saída VC da vogal anterior. O planner deve aproveitar uma
transição gravada disponível, sem duplicar ataques já presentes em VCV,
clusters completos ou caudas nasais e sem programar aliases ausentes.

No áudio CVVC anterior, as bordas internas de algumas pausas de pontuação
terminavam em amostras não nulas, seguidas de silêncio digital. Uma suavização
de quatro milissegundos nessas bordas reduz o corte brusco. Ela deve conservar
a duração e deixar intactas as sobreposições contínuas de fala.

Nomes e exceções lexicais precisam de acento e vogais próprios. Uma regra
genérica de palavra terminada em “u”, por exemplo, não deve deslocar o acento
de “Miku” para a última sílaba. As exceções não justificam transformar todas
as vogais “e” e “o” em abertas.

## Comparação e limites

O comparador executa o renderer anterior e o atual em processos separados,
com o mesmo texto, bank, flags, tom C4, deslocamento zero e velocidade normal.
Ele conserva a configuração do worker e usa caches temporários. Os MP3s
permitem conferir pronúncia, clareza, ritmo, estalos e timbre por audição.

O campo histórico `energy_boundary_max_db` mede diferença de RMS na entrada
dos fragmentos antes do ajuste de junção; não mede a qualidade da frase
resultante. As métricas fonéticas descrevem o plano e não uma transcrição
do áudio. O teste no Linux x64 verifica execução e integridade; a comparação
no Termux verifica o runtime ARM64 do aparelho.
