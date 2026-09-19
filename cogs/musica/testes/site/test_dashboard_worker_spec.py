from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SPEC = ROOT / "cogs/musica/site/dashboard-worker.json"


def test_politica_do_worker_de_musica_mora_na_cog() -> None:
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    assert data["required_capabilities"] == ["phone-worker", "music"]
    assert "apk" in data["excluded_runtime_kinds"]
    assert "core-worker-apk" in data["excluded_source_prefixes"]
    assert int(data["offline_after_seconds"]) >= 15


def test_dashboard_so_mantem_avaliador_generico_e_adaptador_de_caminho() -> None:
    generic = (ROOT / "dashboard/backend/src/services/dashboardWorkerAvailability.ts").read_text(encoding="utf-8")
    service = (ROOT / "dashboard/backend/src/services/dashboardCommandsService.ts").read_text(encoding="utf-8")
    assert '"music"' not in generic
    assert "dashboardWorkerAvailable" in service
    assert 'cogs/musica/site/dashboard-worker.json' in service
    assert not (ROOT / "dashboard/backend/src/services/dashboardMusicWorker.ts").exists()
    assert not (ROOT / "dashboard/backend/tests/dashboard-music-worker.test.ts").exists()
