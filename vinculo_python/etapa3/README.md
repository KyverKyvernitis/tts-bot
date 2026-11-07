# Etapa 3: compilação cruzada com Ubuntu 22.04 existente no celular

## Objetivo

Compilar `tts_fluxo_experimental` para **Linux x86-64** usando CPU ARM64 do celular,
sem alterar o Phone Worker ou o TTS da VPS. Python 3.10+ pela `abi3-py310`.

## Opção A: Ubuntu existente registrado no proot-distro

Na sessão do Termux:

```sh
bash "$HOME/storage/downloads/compilar-tts-rust-etapa3-termux.sh" \
  "$HOME/storage/downloads/nucleo-fluxo-tts-rust-etapa3.zip"
```

A distribuição usada por padrão se chama `ubuntu`. Se seu Ubuntu 22.04 estiver
registrado sob outro nome:

```sh
UBUNTU_TTS_NOME=meu-ubuntu bash "$HOME/storage/downloads/compilar-tts-rust-etapa3-termux.sh" \
  "$HOME/storage/downloads/nucleo-fluxo-tts-rust-etapa3.zip"
```

O script **não instala uma nova distribuição**. Entra temporariamente no Ubuntu
existente e mapeia a pasta de fontes em `/mnt` apenas durante a sessão.

## Opção B: Ubuntu iniciado por outro launcher

Entre no Ubuntu 22.04 pelo seu método habitual e execute:

```sh
bash /sdcard/Download/compilar-tts-rust-ubuntu-existente.sh \
  /sdcard/Download/nucleo-fluxo-tts-rust-etapa3.zip \
  /sdcard/Download
```

Ajuste os caminhos conforme os mounts expostos pelo launcher. Se `/sdcard` não
estiver disponível, copie os dois arquivos para uma pasta do Ubuntu e use o caminho
real. A distribuição deve ser Ubuntu 22.04 ARM64.

## Dependências e isolamento

- `gcc-x86-64-linux-gnu`, `binutils`, `curl`, `python3`, `file`, `ca-certificates`
  e `build-essential` são instalados **somente se ausentes**, após simulação
  que bloqueia remoção de pacotes e alterações de FFmpeg / Wine / Box64.
- Uma toolchain Rust `1.99.0` exclusiva é instalada em
  `~/.local/share/tts-rust-cross-etapa3`, sem modificar o Rust existente.
- Target `x86_64-unknown-linux-gnu`, linker `x86_64-linux-gnu-gcc`.
- PyO3 com `PYO3_NO_PYTHON=1` e `abi3-py310` para compilar sem executar
  o Python x86-64 no celular.
- Verificação de ELF x86-64, símbolo `PyInit__nucleo_fluxo_tts`
  e limite GLIBC 2.35 antes de empacotar.
- `--jobs 2`, `nice`, sem integração automática no bot.

## Validação na VPS

Transfira o arquivo `tts-rust-linux-x86_64-*.zip` da pasta Download para a VPS.
No Ubuntu 22.04 x86-64 da VPS, em pasta **fora do bot**:

```sh
mkdir -p "$HOME/tts-rust-validacao"
unzip -q tts-rust-linux-x86_64-ARQUIVO.zip -d "$HOME/tts-rust-validacao"
bash "$HOME/tts-rust-validacao/linux_x86_64/validar_na_vps.sh"
```

O validador apenas importa a extensão e executa testes, podendo usar o Python
já utilizado no bot. Com `--benchmark`, também executa 20 comparações isoladas.

Esta etapa **não** ativa o Rust no TTS em produção.
