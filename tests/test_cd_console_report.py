"""Terminal summary preserves verification while full saved report stays intact."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('cd_console_cli', HERE / 'import-midi.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class ConsoleReportTests(unittest.TestCase):
    def test_summary_keeps_outcome_hashes_and_audio_validation_without_note_dump(self):
        original = {'ok': True, 'processed': [{'id':'example', 'complete':True, 'sample_status':'ready',
            'zip':'/tmp/example.zip', 'zip_sha256':'a'*64, 'zip_bytes':50000,
            'extraction':{'vocal_status':'ambiguous', 'warnings':['vocal_line_ambiguous'],
                          'note_names':[{'note':'C','octave':4}]*128, 'candidates':[{}]*200},
            'validation':{'ok':True,'songs':['example'],'files':['a','b'], 'package_manifest':{},
                          'artifacts':{'example':{'sample':'ready','audio':{'verified':'decoded'}}}}}],
            'failures':[], 'pending':[], 'published_to_vps':False}
        before = json.dumps(original, sort_keys=True)
        summary = cli.console_report(original, Path('/tmp/full-report.json'))
        row = summary['processed'][0]
        self.assertTrue(row['complete'])
        self.assertEqual(row['zip_sha256'], 'a'*64)
        self.assertEqual(row['extraction']['warnings'], ['vocal_line_ambiguous'])
        self.assertEqual(row['validation']['artifacts']['example']['audio']['verified'], 'decoded')
        self.assertNotIn('note_names', json.dumps(summary))
        self.assertNotIn('candidates', json.dumps(summary))
        self.assertEqual(json.dumps(original, sort_keys=True), before)
        self.assertEqual(summary['full_report'], '/tmp/full-report.json')

    def test_pending_and_failures_are_visible(self):
        original = {'ok':False,'processed':[], 'failures':[{'file':'bad.mid','kind':'permanent'}],
                    'pending':[{'file':'slow.mid','reason':'retry_cooldown'}]}
        summary = cli.console_report(original, Path('/tmp/full.json'))
        self.assertEqual(summary['failures'], original['failures'])
        self.assertEqual(summary['pending'], original['pending'])

    def test_watch_idle_and_pending_polls_keep_last_operation_audit(self):
        completed = {'ok': True, 'processed': [{'id': 'song', 'zip': '/tmp/song.zip',
                    'selection_audit': {'candidates': [{'video_id': 'abcdefghijk', 'matching': True}]}}],
                     'pending': [], 'failures': []}
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'report.json'
            cli.save_report(completed, report, watch=True)
            initial = report.read_bytes()
            for pending in ([], [{'file': 'song.mid', 'reason': 'retry_cooldown'}]):
                latest = {'ok': True, 'processed': [], 'pending': pending, 'failures': []}
                saved = cli.save_report(latest, report, watch=True)
                self.assertTrue(saved['last_report_preserved'])
                self.assertEqual(report.read_bytes(), initial)
                current = json.loads(Path(saved['watch_status']).read_text())
                self.assertEqual(current['processed'], [])
                self.assertEqual(current['pending'], pending)
                self.assertEqual(current['full_report'], str(report.resolve()))
                self.assertIn('observed_at', current)

    def test_watch_new_failure_updates_actionable_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'report.json'
            cli.save_report({'ok': True, 'processed': [{'id': 'song'}]}, report, watch=True)
            failed = {'ok': False, 'processed': [], 'failures': [{'file': 'bad.mid', 'kind': 'permanent'}]}
            saved = cli.save_report(failed, report, watch=True)
            self.assertFalse(saved['last_report_preserved'])
            self.assertEqual(json.loads(report.read_text()), failed)

    def test_one_shot_report_still_describes_current_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'report.json'
            cli.save_report({'ok': True, 'processed': [{'id': 'song'}]}, report)
            idle = {'ok': True, 'processed': [], 'pending': [], 'failures': []}
            saved = cli.save_report(idle, report)
            self.assertFalse(saved['last_report_preserved'])
            self.assertNotIn('watch_status', saved)
            self.assertEqual(json.loads(report.read_text()), idle)


if __name__ == '__main__': unittest.main()
