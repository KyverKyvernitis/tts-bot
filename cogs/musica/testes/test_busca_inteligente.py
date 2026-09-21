from __future__ import annotations

from cogs.musica.busca import analisar_consulta, analisar_estrutura
from cogs.musica.busca.chaves import chave_semantica_busca


def test_intencao_preserva_versao_e_identidade() -> None:
    consulta = analisar_consulta("Arctic Monkeys - 505 live")
    assert consulta.artista == "arctic monkeys"
    assert consulta.titulo.startswith("505")
    assert "live" in consulta.atributos


def test_clean_bandit_nao_vira_falso_pedido_de_clean_version() -> None:
    consulta = analisar_consulta("Clean Bandit - Rather Be")
    assert consulta.artista == "clean bandit"
    assert "clean" not in consulta.atributos


def test_estrutura_entende_titulo_by_artista() -> None:
    estrutura = analisar_estrutura("Numb by Linkin Park")
    assert estrutura.titulo == "numb"
    assert estrutura.artista == "linkin park"


def test_estrutura_usa_ultimo_by_quando_titulo_contem_by() -> None:
    estrutura = analisar_estrutura("Stand by Me by Ben E. King")
    assert estrutura.titulo == "stand by me"
    assert estrutura.artista == "ben e king"


def test_estrutura_extrai_feat_sem_poluir_titulo() -> None:
    consulta = analisar_consulta("Song feat. Guest - Artist")
    assert "guest" in consulta.colaboradores
    assert "feat" not in consulta.titulo.lower()


def test_banda_live_nao_vira_falso_pedido_de_versao_ao_vivo() -> None:
    consulta = analisar_consulta("Live - Lightning Crashes")
    assert consulta.artista == "live"
    assert "live" not in consulta.atributos


def test_chave_semantica_unifica_formas_equivalentes() -> None:
    assert chave_semantica_busca("Linkin Park - Numb") == chave_semantica_busca(
        "Numb by Linkin Park"
    )


def test_chave_semantica_mantem_qualificadores_distintos() -> None:
    assert chave_semantica_busca("Numb live") != chave_semantica_busca("Numb lyrics")
