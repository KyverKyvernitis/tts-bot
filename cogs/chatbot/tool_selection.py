"""Seleção local de contratos nativos, com descoberta escolhida pelo modelo.

O índice completo continua visível. O ranking usa o texto do próprio catálogo,
não reconhece comandos nem executa intenções. Uma seleção ruim pode ser
corrigida com carregar_ferramentas sem outro classificador de IA.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import replace


DISCOVERY_TOOL = "carregar_ferramentas"
INITIAL_TOOL_LIMIT = 5
INITIAL_SCHEMA_CHARS = 4800
_CORE = (DISCOVERY_TOOL,)
_STOP = frozenset("""a ao aos as ate com como da das de do dos e em entre era essa
esse esta estas este eu foi ha isso isto la mais mas me meu meus minha minhas
na nas nao no nos o os ou para pela pelo por porque qual quando que quem se
sem seu seus sim so sua suas tem ter um uma umas uns voce voces the a an and
are as at be can do for from how in is it of on or that the this to use with
""".split())


def text_terms(text):
    """Vocabulário barato, sem dependências ou rede; acentos são equivalentes."""
    normalized = unicodedata.normalize("NFKD", str(text or "")[:12000].casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return frozenset(term for term in re.findall(r"[a-z0-9]+", normalized)
                     if len(term) >= 3 and term not in _STOP and not term.isdigit())


def _metadata_text(spec):
    # IDs/refs dinâmicos não participam do ranking nem se tornam uma intenção.
    parts = [spec.name.replace("_", " "), spec.description]

    def collect(node, field=""):
        if not isinstance(node, dict):
            return
        if isinstance(node.get("description"), str):
            parts.append(node["description"])
        if field not in {"target_ref", "message_id", "ref", "request_id"}:
            parts.extend(str(item).replace("_", " ") for item in node.get("enum", ())
                         if isinstance(item, str))
        for name, child in node.get("properties", {}).items():
            collect(child, name)
        collect(node.get("items"), field)

    collect(spec.parameters)
    return " ".join(parts)


def declaration_chars(spec):
    """Métrica de tamanho, não uma estimativa de tokens faturados."""
    return len(json.dumps(spec.native_declaration(), ensure_ascii=False, separators=(",", ":")))


class ToolSelection:
    def __init__(self, registry, query="", recent_context="", *,
                 max_initial=INITIAL_TOOL_LIMIT, max_chars=INITIAL_SCHEMA_CHARS):
        self.registry = registry
        self._selected = set()
        self._used = {}
        # Nomes carregados explicitamente pelo próprio modelo precisam sobreviver
        # à próxima rodada. Já os candidatos especulativos do ranking inicial
        # podem ser descartados depois do primeiro lote se não foram usados.
        self._explicit = set()
        # Candidatos relevantes que não couberam no orçamento inicial. Seus
        # nomes já aparecem no enum de carregar_ferramentas; guardamos poucos
        # hints descritivos para não repetir o catálogo inteiro no system.
        self._index_hints = ()
        self._query_terms = frozenset()
        self._relevant = frozenset()
        # Adapters de teste/legados sem descoberta mantêm seu contrato integral.
        self.enabled = registry.get(DISCOVERY_TOOL) is not None
        specs = tuple(registry.get_specs())
        if not self.enabled:
            self._selected.update(spec.name for spec in specs)
        else:
            self._selected.update(name for name in _CORE if any(spec.name == name for spec in specs))
            documents = {spec.name: text_terms(_metadata_text(spec)) for spec in specs}
            current, recent = text_terms(query), text_terms(str(recent_context)[-1600:])
            self._query_terms = current
            counts = {term: sum(term in terms for terms in documents.values())
                      for term in current | recent}

            def score(spec):
                terms = documents[spec.name]
                # Termos específicos do catálogo valem mais que "conversa".
                return sum((3.0 if term in current else .65)
                           * math.log1p(len(specs) / max(1, counts[term]))
                           for term in terms & (current | recent))

            size = sum(declaration_chars(spec) for spec in specs if spec.name in self._selected)
            scored = [(score(spec), spec) for spec in specs]
            self._relevant = frozenset(spec.name for relevance, spec in scored if relevance > 0)
            ranked = sorted(scored, key=lambda item: (-item[0], item[1].name))
            omitted_relevant = []
            for relevance, spec in ranked:
                if spec.name in self._selected or relevance <= 0:
                    continue
                cost = declaration_chars(spec)
                if len(self._selected) >= max(1, int(max_initial)) or size + cost > max(1, int(max_chars)):
                    omitted_relevant.append(spec.name)
                    continue
                self._selected.add(spec.name)
                size += cost
            # Só candidatos que realmente combinaram com a mensagem precisam de
            # descrição fora dos schemas. O enum da ferramenta de descoberta
            # já contém todos os demais nomes reais do catálogo.
            self._index_hints = tuple(omitted_relevant[:6])
        registry.selection = self

    @property
    def selected_names(self):
        return tuple(spec.name for spec in self.get_specs())

    def trivial_text_only_candidate(self):
        """True só para fala sem termos úteis e sem afinidade com o catálogo.

        É um atalho conservador, não um classificador de intenção: mensagens com
        qualquer termo lexical real continuam passando pelo fluxo de tools. O
        contexto recente também participa de ``_relevant``, então confirmações
        curtas de um fluxo operacional não perdem os contratos necessários.
        """
        return bool(self.enabled and not self._query_terms and not self._relevant)

    def index_hints(self):
        """Nomes relevantes omitidos dos schemas por limite de tamanho/quantidade."""
        return tuple(name for name in self._index_hints if name not in self._selected)

    def mark_used(self, names):
        if isinstance(names, str):
            names = (names,)
        available = {spec.name: spec for spec in self.registry.snapshot()}
        for name in names:
            spec = available.get(name)
            if spec is not None:
                self._selected.add(name)
                self._used[name] = spec

    def load(self, names):
        snapshot = {spec.name: spec for spec in self.registry.snapshot()}
        loaded, unavailable = [], {}
        for name in dict.fromkeys(names):
            spec = snapshot.get(name)
            if spec is None or not spec.available:
                # O schema restringe nomes ao catálogo. Nenhum nome arbitrário
                # vindo de documentos pode ser anunciado como uma ferramenta.
                if spec is not None:
                    unavailable[name] = spec.why[:180] or "Indisponível neste turno."
                continue
            self._selected.add(name)
            self._explicit.add(name)
            loaded.append(name)
        return {"loaded": loaded, "unavailable": unavailable}

    def prune_speculative(self):
        """Descarta schemas só sugeridos pelo ranking e nunca usados/carregados.

        O catálogo continua acessível por ``carregar_ferramentas``. Contratos de
        chamadas já emitidas e contratos explicitamente carregados permanecem
        declarados para preservar o histórico nativo e a próxima decisão.
        """
        if not self.enabled:
            return ()
        keep = set(_CORE) | set(self._used) | set(self._explicit)
        removed = tuple(sorted(name for name in self._selected if name not in keep))
        self._selected.intersection_update(keep)
        return removed

    def get_specs(self):
        selected = []
        for spec in self.registry.snapshot():
            if spec.name not in self._selected:
                continue
            if spec.available:
                selected.append(spec)
            elif spec.name in self._used:
                # O contrato da chamada passada precisa continuar no histórico
                # nativo do Gemini/fallback. Disponibilidade para NOVA execução
                # vem do snapshot/guard do host, nunca desta declaração retida.
                selected.append(replace(self._used[spec.name], available=True))
        return tuple(selected)

    def availability_state(self):
        snapshot = self.registry.snapshot()
        selected = set(self.selected_names)
        # Os contratos nativos já dizem ao modelo quais ferramentas estão
        # carregadas e quais enums aceitam. No estado dinâmico só precisamos
        # registrar uma indisponibilidade que mudou desde a seleção.
        unavailable = {spec.name: spec.why[:180] or "Indisponível neste turno."
                       for spec in snapshot if not spec.available and spec.name in selected}
        return {"unavailable": unavailable} if unavailable else {}

    def metrics(self):
        full = tuple(self.registry.get_specs())
        selected = self.get_specs()
        return {"catalog_tools": len(full), "loaded_tools": len(selected),
                "catalog_schema_chars": sum(declaration_chars(spec) for spec in full),
                "loaded_schema_chars": sum(declaration_chars(spec) for spec in selected),
                "explicit_tools": len(self._explicit),
                "used_tools": len(self._used),
                "index_hint_tools": len(self.index_hints())}
