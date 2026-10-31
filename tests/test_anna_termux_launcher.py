"""The install kit restarts only the configured worker after a successful install."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('arguments,status,restarts', [([], 0, True),
    (['--download', '--dry-run'], 0, False), (['--fail'], 7, False)])
def test_kit_launcher_preserves_arguments_and_does_not_restart_after_failure(tmp_path, arguments, status, restarts):
    launcher = tmp_path / 'instalar-anna.sh'
    shutil.copyfile(ROOT / 'deploy/anna-utau/termux/install-anna.sh', launcher)
    installer = tmp_path / 'deploy/anna-utau/termux/install-voicebank.py'
    installer.parent.mkdir(parents=True)
    installer.write_text('''import json,os,sys
from pathlib import Path
Path(os.environ['ANNA_TEST_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))
raise SystemExit(7 if '--fail' in sys.argv else 0)
''')
    worker = tmp_path / 'worker com espaço'
    worker.mkdir()
    (worker / 'start-phone-worker.sh').write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$1" > "$ANNA_TEST_RESTART"\n')
    args_file, restarted = tmp_path / 'arguments.json', tmp_path / 'restart'
    env = dict(os.environ, PHONE_WORKER_DIR=str(worker), ANNA_TEST_ARGUMENTS=str(args_file),
               ANNA_TEST_RESTART=str(restarted))
    result = subprocess.run(['bash', str(launcher), *arguments], env=env, capture_output=True, timeout=10)
    assert result.returncode == status, result.stderr.decode()
    assert json.loads(args_file.read_text()) == (arguments or ['--download'])
    assert restarted.exists() == restarts
    if restarts:
        assert restarted.read_text() == '--restart'
