# Painel de canto

Use `_cd` para abrir suas melodias. O comando usa o prefixo do bot configurado
no servidor: com prefixo `!`, por exemplo, o comando é `!cd`.

O painel usa Components V2 e oferece, nesta versão, somente **Padrão**:
C4, E4, G4 e E4, o ciclo de quatro segundos já usado no TTV. A opção original
mostra o avatar atual do bot. Novas músicas poderão ser adicionadas ao catálogo
do bot; não existem opções de músicas ainda indisponíveis no seletor.

**Ouvir amostra** envia um MP3 privado, cantado pela vocaloid e no idioma
selecionados no TTV. A amostra usa as notas e o andamento da melodia, sem
alterar os ajustes de tom e velocidade da fala. Ela não entra na fila nem
conecta o bot à call. O worker de síntese precisa estar disponível para ouvir.

**Usar melodia** confirma a seleção pessoal neste servidor. A navegação pelo
seletor não salva ajustes. Uma opção já em uso aparece confirmada; a escolha
permanece após reiniciar o bot. Abrir, selecionar e salvar continuam disponíveis
com o worker offline. Somente quem abriu o painel pode usar seus controles.

O canto com `'/cantar sua letra` ou `~` separado usa a melodia pessoal. Um MIDI
anexado substitui a escolha somente naquela mensagem. A fala comum mantém
seu comportamento, e a melodia do canto que já entrou na fila permanece igual.

O painel expira após cinco minutos; use `_cd` novamente. Reabrir substitui
somente o seu próprio painel de canto nesse servidor. Esta mudança mantém o
runtime do worker 1.11.41 e não instala bancos, motores, MIDIs ou imagens novos
no Termux.
