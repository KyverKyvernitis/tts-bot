# Music Agent 0.3.85 — continuidade de áudio no Termux

O usuário confirmou que nunca ativou voz na VPS. A voz sempre permaneceu no
Termux; mudar o destino de execução não explica os travamentos relatados.
Esta entrega mantém o mesmo executor e corrige uma regressão introduzida na
preparação antecipada de áudio da 0.3.83.

## Disputa entre preparação e playback

A cota global de decoders extras era compartilhada pela próxima música e pela
próxima parte de uma música arquivada. Uma próxima música já pronta mantinha
sua vaga até ser adotada. Enquanto isso, a faixa atual podia chegar ao fim de
uma parte sem conseguir abrir a seguinte, produzindo silêncio e, depois,
timeout. A disputa também podia ocorrer entre calls diferentes.

A próxima parte da música em reprodução pode cancelar o aquecimento opcional
de uma faixa futura. A limpeza ocorre fora da thread de voz, e a vaga só fica
livre depois de o decoder ser encerrado. Quando a parte atual já chegou ao EOF,
o decoder seguinte substitui o encerrado, sem disputar a cota de fontes extras.
Isso permite continuar mesmo quando outra call mantém uma parte antecipada
pronta. Se o carregamento só começar no EOF, ainda pode haver espera pelo CDN.

A correção é validada com fontes pequenas e determinísticas que
reproduzem essa disputa, incluindo mais de uma call e falhas na parte seguinte,
e com decodificação real por FFmpeg, preservando todos os bytes PCM.
Os detalhes finais da implementação e dos testes constam na
[validação 0.3.85](VALIDACAO_MUSIC_AGENT_0_3_85.md).

## BrokenPipe

A recuperação do proxy HTTP local da 0.3.84 também se aplica ao Termux: os
comandos do controller chegam ao Phone Worker, que os entrega ao Music Agent
no próprio telefone. Um socket HTTP reaproveitado e já fechado pode falhar nesse
percurso independentemente de onde esteja o controller.

Também foi reproduzida uma falha de logging na ponte de resolução: se stdout
estiver fechado, um log podia lançar BrokenPipe antes de retornar o resultado
normal da resolução. A entrega protege esses logs, como já fazia para os logs
do Music Agent desde a 0.3.84.

Repetir o mesmo ID recupera uma entrega perdida e preserva a deduplicação.
Falhas internas HTTP não são reenviadas automaticamente. Se o agente já
executou e falhou ao tocar, repetir a entrega pode apenas devolver o resultado
guardado; recuperação de transporte não garante recuperação do áudio.

O print fornecido não identifica qual pipe falhou. A regressão de continuidade
e o erro de logging foram reproduzidos localmente; ainda não há traceback nem
medição da call do usuário para associar cada sintoma a um desses defeitos.

## Aplicação

Atualize controller e runtime do Phone Worker para 0.3.85 e reinicie pelo
procedimento existente, mantendo os tokens e endereços instalados. A voz
continua no Termux, com `MUSIC_AGENT_VOICE_EXECUTOR=termux` como padrão.

O fórum atual continua sendo o acervo permanente de música. Diretórios
temporários, catálogo durável, aprendizagem, prioridade de arquivos conhecidos
e demais ganhos das versões anteriores são mantidos. A qualidade, os efeitos
e o volume não são alterados por esta correção.
