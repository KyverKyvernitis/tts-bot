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
INITIAL_TOOL_LIMIT = 7
INITIAL_SCHEMA_CHARS = 8500
_CORE = (DISCOVERY_TOOL, "select_response_format", "preparar_resposta")
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
        # Adapters de teste/legados sem descoberta mantêm seu contrato integral.
        self.enabled = registry.get(DISCOVERY_TOOL) is not None
        specs = tuple(registry.get_specs())
        if not self.enabled:
            self._selected.update(spec.name for spec in specs)
        else:
            self._selected.update(name for name in _CORE if any(spec.name == name for spec in specs))
            documents = {spec.name: text_terms(_metadata_text(spec)) for spec in specs}
            current, recent = text_terms(query), text_terms(str(recent_context)[-1600:])
            counts = {term: sum(term in terms for terms in documents.values())
                      for term in current | recent}

            def score(spec):
                terms = documents[spec.name]
                # Termos específicos do catálogo valem mais que "conversa".
                return sum((3.0 if term in current else .65)
                           * math.log1p(len(specs) / max(1, counts[term]))
                           for term in terms & (current | recent))

            size = sum(declaration_chars(spec) for spec in specs if spec.name in self._selected)
            ranked = sorted(specs, key=lambda spec: (-score(spec), spec.name))
            for spec in ranked:
                if spec.name in self._selected or score(spec) <= 0:
                    continue
                cost = declaration_chars(spec)
                if len(self._selected) >= max(1, int(max_initial)) or size + cost > max(1, int(max_chars)):
                    continue
                self._selected.add(spec.name)
                size += cost
        registry.selection = self

    @property
    def selected_names(self):
        return tuple(spec.name for spec in self.get_specs())

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
            loaded.append(name)
        return {"loaded": loaded, "unavailable": unavailable}

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
        state = {"loaded": list(self.selected_names),
                 "unavailable": {spec.name: spec.why[:180] or "Indisponível neste turno."
                                 for spec in snapshot if not spec.available}}
        for spec in snapshot:
            if getattr(spec, "capabilities", ()):
                state[spec.name + "_actions"] = (list(spec.parameters.get("properties", {})
                                                    .get("action", {}).get("enum", ()))
                                                  if spec.available else [])
        return state

    def metrics(self):
        full = tuple(self.registry.get_specs())
        selected = self.get_specs()
        return {"catalog_tools": len(full), "loaded_tools": len(selected),
                "catalog_schema_chars": sum(declaration_chars(spec) for spec in full),
                "loaded_schema_chars": sum(declaration_chars(spec) for spec in selected)}
