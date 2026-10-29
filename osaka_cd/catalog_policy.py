"""Explicit CD removals shared by catalog readers and saved selections."""
from __future__ import annotations


RETIRED_CD_IDS = frozenset({'rolling-girl', 'ghost-rule', 'override'})


def is_retired_cd(identity: object) -> bool:
    """Match catalog identities exactly; similarly named imports stay available."""
    return isinstance(identity, str) and identity.strip() in RETIRED_CD_IDS
