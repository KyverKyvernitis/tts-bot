# Core Linux embedded runner assets

Este diretório documenta os nomes esperados para os componentes nativos futuros.
Não coloque placeholders `.so` aqui e não marque componente falso como pronto.

O preflight só aceita binários reais empacotados em `src/main/jniLibs/arm64-v8a`
e expostos pelo Android em `nativeLibraryDir`. Arquivos baixados/importados no
app home continuam bloqueados para execução futura.
