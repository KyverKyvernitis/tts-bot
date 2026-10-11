# Comparação de pronúncia e sonoridade do TTV

O kit gera pares **anterior / atual** com a mesma voicebank, frase, tom e velocidade. Cada versão do renderer roda em um processo Python separado. Os MP3s são codificados em 192 kb/s e verificados por decodificação; os WAVs também ficam disponíveis para comparação sem compressão.

```sh
unzip -o "$HOME/storage/downloads/ttv-qualidade-termux-kit-v1.zip" \
  -d "$HOME/ttv-qualidade-termux-kit"

python "$HOME/ttv-qualidade-termux-kit/deploy/ttv-quality/termux/compare-quality.py" \
  --container voicepeak-arm64
```

São quatro MP3s: uma frase curta em português adaptado, antes e depois, para cada voz. O programa informa a subpasta criada em `Download/ttv-qualidade`. Os nomes identificam a voz, o idioma e a versão. Cada execução cria uma pasta nova para conservar comparações anteriores. Se o gerenciador de arquivos não atualizar a lista, feche e abra a pasta novamente; o kit solicita uma atualização do índice de mídia quando o Termux:API está instalado.

Para testar também inglês e frases com mais nasais e encontros consonantais:

```sh
python "$HOME/ttv-qualidade-termux-kit/deploy/ttv-quality/termux/compare-quality.py" \
  --container voicepeak-arm64 --english --extended
```

Esse teste completo gera 16 MP3s e pode demorar alguns minutos. O limite é de 120 segundos por amostra. `--voice miku` ou `--voice teto` restringe o teste a uma voz. Se a bank não estiver na pasta configurada, informe `--miku-voicebank /caminho/da/bank` ou `--teto-voicebank /caminho/da/bank`. `--esper-root "$HOME/.esper-utau"` permite selecionar explicitamente o runtime já instalado.

As duas versões usam C4, deslocamento de tom zero e velocidade 1,0. Flags, tempo, velocity e modulation são preservados separadamente da configuração de cada voz. A Miku mantém quatro renderizações paralelas; a Teto mantém duas. A escolha de voz, idioma, tom e velocidade dos usuários do bot não é alterada.

O arquivo `.phone-worker.env` é lido como texto, sem executar comandos, e não é modificado. Não há download, substituição da ponte ESPER ou mudança nas voicebanks. A ponte antiga do ESPER, sem `--implementation-id`, é reconhecida. A análise ESPER usa um cache temporário compartilhado; os fragmentos de áudio ficam separados por voz e versão. Os caches de produção permanecem intactos, e os caches temporários são apagados ao final, inclusive quando uma amostra falha.

O JSON registra arquivos, versões, controles e falhas de cada amostra. Cobertura de aliases e telemetria não medem naturalidade ou inteligibilidade. Compare pela audição os pares da mesma voz e frase, mantendo o volume igual. O inglês da voicebank ajuda a adaptar os sons; ele não transforma a bank em uma gravação nativa de português.

O ZIP completo precisa conter o comparador, os helpers `deploy/miku-utau/termux/install-voicebank.py` e `validate-miku.py`, a cópia anterior em `deploy/ttv-quality/baseline/phone-worker/teto_renderer` e a versão atual em `deploy/termux/phone-worker/teto_renderer`. Esses arquivos ficam no kit de comparação; a cópia anterior não faz parte do runtime de produção.
