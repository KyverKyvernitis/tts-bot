"""Durable publication, private configuration and completion semantics."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'))
import cd_publish as publication


class PublicationOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.zip = self.root / 'cd.zip'
        self.zip.write_bytes(b'validated-fixture-bytes')
        self.digest = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        self.config = {'publish_url': 'https://example.test/api/osaka/cd/publish', 'token': 'private-test-token-with-minimum-32-characters'}
        self.result = {'id': 'example', 'zip': str(self.zip), 'zip_sha256': self.digest}

    def tearDown(self):
        self.temporary.cleanup()

    def test_config_is_private_and_environment_override_never_prints_secret(self):
        path = self.root / 'config.json'
        report = publication.save_configuration(self.config['publish_url'], self.config['token'], path)
        self.assertNotIn(self.config['token'], json.dumps(report))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(publication.load_configuration(path), self.config)
        path.chmod(0o644)
        with self.assertRaisesRegex(publication.PublishError, 'private_owned'):
            publication.load_configuration(path)

    def test_config_symlink_and_cross_origin_status_refused(self):
        path = self.root / 'config.json'
        path.symlink_to(self.zip)
        with self.assertRaises(publication.PublishError):
            publication.save_configuration(self.config['publish_url'], self.config['token'], path)
        for status in ('https://other.test/api/osaka/cd/jobs/fixture', '/other/endpoint', '//other.test/api/osaka/cd/jobs/fixture'):
            with self.assertRaises(publication.PublishError):
                publication._status_url(self.config['publish_url'], status)

    def test_outbox_survives_receipt_and_applied_until_activation(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        self.assertIn(self.digest, json.loads(box.path.read_text()))
        received = {'state': 'received', 'job_id': 'a'*64, 'status_url': 'https://example.test/api/osaka/cd/jobs/' + 'a'*64,
                    'installed': False, 'published_to_vps': True}
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(publication, 'publish_package', return_value=received) as upload:
            self.assertFalse(box.process(force=True)[0]['installed'])
        self.assertEqual(upload.call_args.kwargs['expected_versions'], {'example': None})
        box = publication.PublicationOutbox(self.root, self.config)
        with patch.object(publication, 'publication_status', return_value={**received, 'state': 'applied', 'applied': True}), \
             patch.object(publication, 'publish_package', side_effect=AssertionError('do not upload accepted job again')):
            self.assertFalse(box.process(force=True)[0]['installed'])
        self.assertIn(self.digest, box.jobs)
        with patch.object(publication, 'publication_status', return_value={**received, 'state': 'activated', 'applied': True, 'installed': True}):
            self.assertTrue(box.process(force=True)[0]['installed'])
        self.assertEqual(publication.PublicationOutbox(self.root, self.config).jobs, {})

    def test_network_failure_retains_cas_and_exact_package_for_retry(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {'example': 'b'*64}}), \
             patch.object(publication, 'publish_package', side_effect=OSError('contains-secret-never-print')):
            report = box.process(force=True)[0]
        self.assertEqual(report['error'], 'publication_OSError')
        self.assertNotIn('contains-secret', json.dumps(report))
        with patch.object(publication, 'publication_status', side_effect=AssertionError('CAS is durable')), \
             patch.object(publication, 'publish_package', return_value={'state': 'activated', 'installed': True, 'published_to_vps': True}) as upload:
            box.process(force=True)
        self.assertEqual(upload.call_args.kwargs['expected_versions'], {'example': 'b'*64})

    def test_legacy_receipt_is_kept_without_false_installation(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(publication, 'publish_package', return_value={'state': 'queued', 'installed': False, 'published_to_vps': True, 'candidate_id': 'fixture'}):
            box.process(force=True)
        with patch.object(publication, 'publish_package', side_effect=AssertionError('already accepted')):
            report = box.process(force=True)[0]
        self.assertEqual(report['reason'], 'legacy_updater_receipt_requires_verification')
        self.assertIn(self.digest, box.jobs)

    def test_manual_failed_retry_refreshes_cas_and_survives_network_failure(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        box.jobs[self.digest].update(state='failed', job_id='a'*64, published_to_vps=True,
            status_url='https://example.test/api/osaka/cd/jobs/'+'a'*64,
            expected_versions={'example': None})
        box.save()
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {'example': 'b'*64}}), \
             patch.object(publication, 'publish_package', side_effect=OSError('offline')) as upload:
            box.process(force=True)
        self.assertTrue(upload.call_args.kwargs['retry_failed'])
        self.assertEqual(upload.call_args.kwargs['expected_versions'], {'example': 'b'*64})
        self.assertTrue(publication.PublicationOutbox(self.root, self.config).jobs[self.digest]['retry_failed'])
        box = publication.PublicationOutbox(self.root, self.config)
        with patch.object(publication, 'publication_status', side_effect=AssertionError('reuse durable CAS')), \
             patch.object(publication, 'publish_package', return_value={'state':'activated','installed':True,'published_to_vps':True}) as upload:
            box.process(force=True)
        self.assertTrue(upload.call_args.kwargs['retry_failed'])
        self.assertEqual(box.jobs, {})

    def test_new_unsent_package_supersedes_only_unsent_song_revision(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        new = {**self.result, 'zip_sha256': 'f'*64}
        box.enqueue(new)
        self.assertEqual(set(box.jobs), {'f'*64})
        box.jobs['f'*64]['job_id'] = 'a'*64
        box.enqueue(self.result)
        self.assertEqual(set(box.jobs), {'f'*64, self.digest})

    def test_activated_requires_explicit_installation_ack(self):
        url = self.config['publish_url']
        self.assertFalse(publication._receipt({'ok': True, 'state': 'applied', 'job_id': 'a'*64, 'installed': True}, url)['installed'])
        self.assertFalse(publication._receipt({'ok': True, 'state': 'activated', 'job_id': 'a'*64}, url)['installed'])
        self.assertTrue(publication._receipt({'ok': True, 'state': 'activated', 'job_id': 'a'*64, 'installed': True}, url)['installed'])

    def test_cooldown_entries_do_not_starve_later_ready_publications(self):
        box = publication.PublicationOutbox(self.root, self.config)
        for index in range(8):
            box.jobs[str(index)] = {**self.result, 'state': 'failed', 'retry_after': 10**12}
        box.enqueue(self.result)
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(publication, 'publish_package', return_value={'state': 'activated', 'installed': True, 'published_to_vps': True}) as upload:
            reports = box.process()
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(len(reports), 1)
        self.assertNotIn(self.digest, box.jobs)

    def test_completed_receipt_survives_restart_and_does_not_reupload(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(publication, 'publish_package', return_value={'state': 'activated', 'installed': True, 'published_to_vps': True}):
            box.process(force=True)
        restarted = publication.PublicationOutbox(self.root, self.config)
        restarted.enqueue(self.result)
        self.assertEqual(restarted.jobs, {})
        self.assertIn(self.digest, restarted.completed)

    def test_same_song_waits_for_previous_publication_to_finish_before_reading_cas(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        box.jobs[self.digest].update(state='processing', published_to_vps=True, job_id='a'*64,
            status_url='https://example.test/api/osaka/cd/jobs/'+'a'*64, retry_after=10**12)
        second_zip = self.root / 'second.zip'
        second_zip.write_bytes(b'new-validated-package')
        second = {'id': 'example', 'zip': str(second_zip), 'zip_sha256': hashlib.sha256(second_zip.read_bytes()).hexdigest()}
        box.enqueue(second)
        with patch.object(publication, 'publication_status', side_effect=AssertionError('previous accepted job is cooling down')), \
             patch.object(publication, 'publish_package', side_effect=AssertionError('wait for prior generation')):
            self.assertEqual(box.process(), [])
        prior = {'state': 'activated', 'installed': True, 'published_to_vps': True}
        with patch.object(publication, 'publication_status', side_effect=[prior, {'ok': True, 'versions': {'example': 'b'*64}}]), \
             patch.object(publication, 'publish_package', return_value=prior) as upload:
            reports = box.process(force=True)
        self.assertEqual(len(reports), 2)
        self.assertEqual(upload.call_args.kwargs['expected_versions'], {'example': 'b'*64})
        self.assertEqual(box.jobs, {})

    def test_small_watch_budget_rotates_due_jobs_without_polling_one_forever(self):
        box = publication.PublicationOutbox(self.root, self.config)
        box.enqueue(self.result)
        first = {'state': 'received', 'job_id': 'a'*64, 'published_to_vps': True, 'installed': False,
                 'status_url': 'https://example.test/api/osaka/cd/jobs/'+'a'*64}
        with patch.object(publication, 'publication_status', return_value={'ok': True, 'versions': {}}), \
             patch.object(publication, 'publish_package', return_value=first):
            box.process(force=True, limit=1)
        second = {**self.result, 'id': 'other-song', 'zip_sha256': 'f'*64}
        box.enqueue(second)
        with patch.object(publication, 'publication_status', side_effect=AssertionError('new due song receives next turn')), \
             patch.object(publication, 'publish_package', side_effect=AssertionError('placeholder checksum refused')):
            report = box.process(limit=1)
        self.assertEqual(report[0]['id'], 'other-song')
        self.assertEqual(report[0]['error'], 'publication_package_missing_or_changed')


if __name__ == '__main__':
    unittest.main()
