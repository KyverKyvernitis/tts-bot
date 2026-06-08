# Patch tickets — modais V2 no editor

- Troca os campos manuais de `ticketedit > Opções` por Checkbox Group e Checkboxes quando a versão do `discord.py` suportar Components V2 em modais.
- Troca `ticketedit > Canais` por Channel Select com filtro por tipo de canal.
- Troca `ticketedit > Cargos` por Role Select, evitando copiar IDs manualmente.
- Melhora o modal de denúncia com String Select para o tipo da denúncia e User Select opcional para o usuário denunciado.
- Mantém fallback automático para TextInput caso a lib/cliente não aceite algum componente novo no modal.
