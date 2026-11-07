#!/usr/bin/env python3
"""SIMULATED publication-policy fixtures; no GitHub, Homebrew or package acceptance."""
import argparse
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('personal_release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = '2026.11.7.23.1'
SOURCE = 'a' * 40


def fixture(directory):
    """Deliberately inert payloads test metadata policy, never package behavior."""
    m = dict(schema=1, version=VERSION, tag='personal-' + VERSION,
             source_sha=SOURCE, upstream_sha=release.UPSTREAM, upstream_version='3.8',
             workflow=dict(run_id='90', run_number='23', run_attempt='1',
                           created_at='2026-11-07T09:01:02Z'), targets={})
    source = f'tmux-{VERSION}.tar.gz'
    with tarfile.open(directory / source, 'w:gz') as archive:
        marker = tarfile.TarInfo(f'tmux-{VERSION}/.personal-source-sha')
        data = (SOURCE + '\n').encode()
        marker.size = len(data)
        archive.addfile(marker, io.BytesIO(data))
    m['source'] = dict(asset=source, sha256=release.digest(directory / source))
    for target in sorted(release.TARGETS):
        if target == 'macos-26-arm64':
            record = dict(asset=f'tmux-{VERSION}.arm64_tahoe.bottle.tar.gz',
                          formula='cosgroveb/tap/tmux', version=VERSION,
                          bottle_tag='arm64_tahoe', verified=True)
        else:
            _, suite, arch = target.split('-')
            deb_version = VERSION + '-1+' + suite
            record = dict(asset=f'tmux_{deb_version}_{arch}.deb', package='tmux',
                          version=deb_version, suite=suite, architecture=arch,
                          binary_sha256='b' * 64, verified=True)
        (directory / record['asset']).write_bytes(('SIMULATED payload ' + target).encode())
        record['sha256'] = release.digest(directory / record['asset'])
        m['targets'][target] = record
    root = f'https://github.com/cosgroveb/tmux/releases/download/{m["tag"]}'
    (directory / 'tmux.rb').write_text(
        f'class Tmux < Formula\n  version "{VERSION}"\n'
        f'  url "{root}/{source}"\n  sha256 "{m["source"]["sha256"]}"\n'
        f'  bottle do\n    root_url "{root}"\n'
        f'    sha256 cellar: :any, arm64_tahoe: "{m["targets"]["macos-26-arm64"]["sha256"]}"\n  end\n'
        f'  # SIMULATED identity: {SOURCE} {release.UPSTREAM}\nend\n')
    save_manifest(directory, m)
    return m


def save_manifest(directory, m):
    release.write(directory / 'manifest.json', m)
    names = ['manifest.json', m['source']['asset']] + [r['asset'] for r in m['targets'].values()]
    (directory / 'SHA256SUMS').write_text(''.join(
        f'{release.digest(directory / name)}  {name}\n' for name in sorted(set(names))))


class SimulatedPublication:
    """Stateful command transport; subprocesses and remote services are simulated."""
    def __init__(self, directory, m):
        self.directory, self.m = directory, m
        self.assets = {}
        self.state = None
        self.head = SOURCE
        self.tag_sha = SOURCE
        self.latest = None
        self.tap_formula = 'class Tmux < Formula\n  version "2026.11.6.22.1"\nend\n'
        self.tap_commit = 'c' * 40
        self.tap_history = {self.tap_commit: self.tap_formula}
        self.checkout = None
        self.checkout_commit = None
        self.committed = None
        self.pushes = 0
        self.fail_pushes = 0
        self.newer_on_push_failure = False
        self.fail_latest_once = False
        self.fail_upload_name = None
        self.events = []
        self.source_changes_after_push = False
        self.newer_tap_after_push = False

    def remote_state(self, tag):
        if self.state is None:
            return None
        return dict(self.state, assets=[dict(name=name) for name in self.assets])

    def latest_version(self):
        return self.latest

    def gh(self, *args):
        if args[:2] == ('release', 'create'):
            self.events.append('stage')
            assert '--latest=false' in args
            self.state = dict(tag_name=args[2], target_commitish=SOURCE)
            return ''
        if args[:2] == ('release', 'upload'):
            path = Path(args[3])
            if path.name == self.fail_upload_name:
                self.fail_upload_name = None
                raise subprocess.CalledProcessError(1, ['SIMULATED upload'])
            assert path.name not in self.assets, 'fixture attempted overwrite'
            self.assets[path.name] = path.read_bytes()
            self.events.append('upload:' + path.name)
            return ''
        if args[:2] == ('release', 'edit'):
            self.events.append('promote')
            if self.fail_latest_once:
                self.fail_latest_once = False
                raise subprocess.CalledProcessError(1, ['SIMULATED promotion'])
            self.latest = self.m['version']
            return ''
        if args[0] == 'api' and '/commits/' in args[1]:
            return self.tag_sha
        if args[0] == 'api' and '/git/ref/heads/personal' in args[1]:
            return self.head
        if args[0] == 'api' and '/contents/Formula/tmux.rb' in args[1]:
            ref = args[1].split('?ref=', 1)[1] if '?ref=' in args[1] else None
            formula = self.tap_history.get(ref, self.tap_formula)
            return base64.b64encode(formula.encode()).decode()
        raise AssertionError(('unexpected simulated gh', args))

    def run(self, *args, **kwargs):
        if args[:2] == ('brew', '--repository'):
            path = self.directory / 'SIMULATED-brew-tap'
            path.mkdir(exist_ok=True)
            return str(path)
        if args[0] == 'brew':
            self.events.append('brew-version-check')
            return 'true'
        if args[0] != 'git':
            raise AssertionError(('unexpected simulated run', args))
        command = args[3:]
        if command[:2] == ('fetch', 'origin'):
            return ''
        if command[:2] == ('reset', '--hard'):
            self.checkout_commit = self.tap_commit
            (self.checkout / 'Formula/tmux.rb').write_text(self.tap_formula)
            return ''
        if command[:1] == ('config',):
            return ''
        if command[:1] == ('add',):
            return ''
        if 'commit' in command:
            self.committed = (self.checkout / 'Formula/tmux.rb').read_text()
            return ''
        if command == ('rev-parse', 'HEAD'):
            return self.checkout_commit
        raise AssertionError(('unexpected simulated git', args))

    def subprocess_run(self, args, **kwargs):
        if args[:2] == ['gh', 'api'] and '/contents/Formula/tmux.rb' in args[2]:
            return subprocess.CompletedProcess(args, 0, json.dumps({'content': base64.b64encode(self.tap_formula.encode()).decode()}), '')
        if args[:3] == ['gh', 'repo', 'clone']:
            self.tap_history[self.tap_commit] = self.tap_formula
            self.checkout = Path(args[4])
            (self.checkout / 'Formula').mkdir(parents=True)
            (self.checkout / 'Formula/tmux.rb').write_text(self.tap_formula)
        elif args[:3] == ['gh', 'release', 'download']:
            name = args[args.index('--pattern') + 1]
            target = Path(args[args.index('--dir') + 1])
            (target / name).write_bytes(self.assets[name])
            self.events.append('download:' + name)
        elif args[0] in ('brew', 'dpkg'):
            self.events.append(args[0] + '-check')
        elif args[0] == 'git':
            command = args[3:]
            if command[:1] == ['diff']:
                changed = (self.checkout / 'Formula/tmux.rb').read_text() != self.tap_formula
                return subprocess.CompletedProcess(args, int(changed))
            if command[:1] == ['push']:
                self.pushes += 1
                if self.fail_pushes:
                    self.fail_pushes -= 1
                    if self.newer_on_push_failure:
                        self.tap_formula = self.tap_formula.replace('2026.11.6.22.1', '2026.11.8.24.1')
                    self.events.append('push-race')
                    return subprocess.CompletedProcess(args, 1)
                self.tap_formula = self.committed
                self.tap_commit = 'd' * 40
                self.checkout_commit = self.tap_commit
                self.tap_history[self.tap_commit] = self.tap_formula
                self.events.append('tap-push')
                if self.source_changes_after_push:
                    self.head = 'e' * 40
                if self.newer_tap_after_push:
                    self.tap_commit = 'f' * 40
                    self.tap_formula = self.tap_formula.replace(VERSION, '2026.11.8.24.1')
                    self.tap_history[self.tap_commit] = self.tap_formula
            else:
                raise AssertionError(('unexpected simulated subprocess git', args))
        else:
            raise AssertionError(('unexpected simulated subprocess', args))
        return subprocess.CompletedProcess(args, 0)

    def patches(self):
        return (patch.dict(os.environ, {'HOMEBREW_TAP_TOKEN': 'SIMULATED-NON-CREDENTIAL'}),
                patch.object(release, 'gh', self.gh),
                patch.object(release, 'run', self.run),
                patch.object(release, 'release_state', self.remote_state),
                patch.object(release, 'latest_version', self.latest_version),
                patch.object(release.subprocess, 'run', self.subprocess_run))


class SimulatedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.m = fixture(self.directory)
        self.args = argparse.Namespace(artifacts=str(self.directory))

    def publication(self):
        remote = SimulatedPublication(self.directory, self.m)
        for context in remote.patches():
            self.enterContext(context)
        return remote

    def reject_manifest(self, mutate, message):
        mutate(self.m)
        save_manifest(self.directory, self.m)
        with self.assertRaisesRegex(ValueError, message):
            release.validate(self.directory)

    def test_retry_identity_uses_original_workflow_date(self):
        output = self.directory / 'retry-identity.json'
        github_output = self.directory / 'github-output'
        original_run = dict(id=1122, run_number=451, run_attempt=1,
                            created_at='2024-02-09T23:59:59Z')
        environment = dict(GITHUB_RUN_ID='1122', GITHUB_RUN_ATTEMPT='7',
                           GITHUB_SHA=SOURCE, GITHUB_OUTPUT=str(github_output))
        with patch.dict(os.environ, environment), patch.object(
                release, 'gh', return_value=json.dumps(original_run)) as api:
            release.identity(argparse.Namespace(output=str(output)))
        api.assert_called_once_with('api', 'repos/cosgroveb/tmux/actions/runs/1122')
        actual = release.read(output)
        self.assertEqual(actual['version'], '2024.2.9.451.7')
        self.assertEqual(actual['tag'], 'personal-2024.2.9.451.7')
        self.assertEqual(actual['source_sha'], SOURCE)
        self.assertEqual(actual['workflow'], dict(run_id='1122', run_number='451',
                         run_attempt='7', created_at='2024-02-09T23:59:59Z'))
        self.assertEqual(github_output.read_text(),
                         'version=2024.2.9.451.7\ntag=personal-2024.2.9.451.7\n')

    def test_assemble_requires_full_matrix(self):
        identity = self.directory / 'identity.json'
        release.write(identity, {key: value for key, value in self.m.items()
                                 if key not in ('source', 'targets')})
        for target, record in self.m['targets'].items():
            release.write(self.directory / (target + '.json'), record)
        (self.directory / 'manifest.json').unlink()
        (self.directory / 'SHA256SUMS').unlink()
        args = argparse.Namespace(identity=str(identity), artifacts=str(self.directory))
        release.assemble(args)
        self.assertEqual(release.validate(self.directory), self.m)
        (self.directory / 'macos-26-arm64.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing target record: macos-26-arm64'):
            release.assemble(args)

    def test_numeric_order_and_invalid_versions(self):
        self.assertGreater(release.version('2026.11.7.100.1'), release.version('2026.11.7.99.9'))
        self.assertGreater(release.version('2026.11.8.1.1'), release.version('2026.11.7.999.9'))
        for bad in ('3.8', '2026.11.07.23.1', '2026.11.7.23', '2026.11.7.23.1/other'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                release.version(bad)

    def test_complete_matrix_and_optional_staging_receipt(self):
        self.assertEqual(release.validate(self.directory), self.m)
        self.assertFalse((self.directory / 'completed.json').exists())
        release.write(self.directory / 'completed.json', dict(schema=1, tag=self.m['tag'],
                      source_sha=SOURCE, manifest_sha256=release.digest(self.directory / 'manifest.json'),
                      tap_commit='c' * 40))
        self.assertEqual(release.validate(self.directory), self.m)

    def test_missing_target(self):
        self.reject_manifest(lambda m: m['targets'].pop('ubuntu-noble-arm64'), 'target matrix')

    def test_unknown_target(self):
        self.reject_manifest(lambda m: m['targets'].update({'freebsd-amd64': copy.deepcopy(next(iter(m['targets'].values())))}), 'target matrix')

    def test_duplicate_json_target_rejected(self):
        path = self.directory / 'manifest.json'
        raw = path.read_text()
        raw = raw.replace('"targets": {', '"targets": {"duplicate": 1,"duplicate": 2,', 1)
        path.write_text(raw)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            release.validate(self.directory)

    def test_tag_mismatch(self):
        self.reject_manifest(lambda m: m.update(tag='personal-2026.11.6.22.1'), 'tag/version')

    def test_source_identity_mismatch(self):
        self.reject_manifest(lambda m: m.update(upstream_sha='f' * 40), 'source identity')

    def test_archive_source_identity_mismatch(self):
        self.reject_manifest(lambda m: m.update(source_sha='f' * 40), 'source archive identity')

    def test_workflow_identity_mismatch(self):
        self.reject_manifest(lambda m: m['workflow'].update(run_attempt='2'), 'workflow/version')

    def test_package_suite_mismatch(self):
        self.reject_manifest(lambda m: m['targets']['debian-bookworm-amd64'].update(suite='trixie'), 'package metadata')

    def test_package_name_mismatch(self):
        self.reject_manifest(lambda m: m['targets']['debian-bookworm-amd64'].update(package='tmux-personal'), 'package metadata')

    def test_unverified_target(self):
        self.reject_manifest(lambda m: m['targets']['macos-26-arm64'].update(verified=False), 'unverified')

    def test_corrupt_payload(self):
        path = self.directory / self.m['source']['asset']
        path.write_bytes(path.read_bytes() + b'corrupted')
        with self.assertRaisesRegex(ValueError, 'payload checksum'):
            release.validate(self.directory)

    def test_duplicate_or_incomplete_checksum_inventory(self):
        path = self.directory / 'SHA256SUMS'
        original = path.read_text()
        path.write_text(original + original.splitlines()[0] + '\n')
        with self.assertRaisesRegex(ValueError, 'duplicate checksum'):
            release.validate(self.directory)
        path.write_text('\n'.join(original.splitlines()[1:]) + '\n')
        with self.assertRaisesRegex(ValueError, 'checksum inventory'):
            release.validate(self.directory)

    def test_receipt_source_mismatch(self):
        release.write(self.directory / 'completed.json', dict(schema=1, tag=self.m['tag'],
                      source_sha='f' * 40, manifest_sha256=release.digest(self.directory / 'manifest.json'),
                      tap_commit='c' * 40))
        with self.assertRaisesRegex(ValueError, 'receipt mismatch'):
            release.validate(self.directory)

    def test_stale_head_and_newer_publications(self):
        for head, latest, tap, error in (
                ('f' * 40, None, None, 'head changed'),
                (SOURCE, '2026.11.8.1.1', None, 'latest is newer'),
                (SOURCE, None, '2026.11.8.1.1', 'tap is newer')):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                release.eligible(self.m, head, latest, tap)
        release.eligible(self.m, SOURCE, VERSION, VERSION)

    def test_first_stage_never_creates_completion_receipt(self):
        remote = self.publication()
        release.stage(self.args)
        self.assertNotIn('completed.json', remote.assets)
        self.assertIsNone(remote.latest)
        self.assertIn('manifest.json', remote.assets)

    def test_partial_upload_reuses_identical_bytes(self):
        remote = self.publication()
        remote.fail_upload_name = 'manifest.json'
        with self.assertRaises(subprocess.CalledProcessError):
            release.stage(self.args)
        first = dict(remote.assets)
        self.assertTrue(first)
        release.stage(self.args)
        for name, data in first.items():
            self.assertEqual(remote.assets[name], data)
            self.assertEqual(remote.events.count('upload:' + name), 1)
        self.assertNotIn('completed.json', remote.assets)

    def test_retagged_source_rejected_before_upload(self):
        remote = self.publication()
        release.stage(self.args)
        original_assets = dict(remote.assets)
        remote.tag_sha = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'release tag source differs'):
            release.stage(self.args)
        self.assertEqual(remote.assets, original_assets)
        self.assertNotIn('tap-push', remote.events)
        self.assertNotIn('promote', remote.events)

    def test_existing_asset_mismatch_rejected(self):
        remote = self.publication()
        release.stage(self.args)
        remote.assets['manifest.json'] = b'SIMULATED wrong immutable asset'
        with self.assertRaisesRegex(ValueError, 'immutable asset differs'):
            release.stage(self.args)
        self.assertNotIn('promote', remote.events)

    def test_missing_tap_token_prevents_staging(self):
        remote = self.publication()
        for environment in ({}, {'HOMEBREW_TAP_TOKEN': ''}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, 'HOMEBREW_TAP_TOKEN is required'):
                    release.publish(self.args)
            self.assertEqual(remote.events, [])
            self.assertEqual(remote.assets, {})
            self.assertIsNone(remote.state)

    def test_publish_and_publication_only_retry_keep_identity(self):
        remote = self.publication()
        release.publish(self.args)
        self.assertIn('download:completed.json', remote.events)
        first_assets = dict(remote.assets)
        first_receipt = release.read(self.directory / 'completed.json')
        release.publish(self.args)
        self.assertEqual(remote.assets, first_assets)
        self.assertEqual(release.read(self.directory / 'completed.json'), first_receipt)
        self.assertEqual(remote.events.count('tap-push'), 1)
        self.assertLess(remote.events.index('tap-push'), remote.events.index('promote'))
        self.assertLess(remote.events.index('promote'), remote.events.index('upload:completed.json'))

    def test_tap_ahead_latest_behind_retry(self):
        remote = self.publication()
        remote.fail_latest_once = True
        with self.assertRaises(subprocess.CalledProcessError):
            release.publish(self.args)
        self.assertIn('tap-push', remote.events)
        self.assertIsNone(remote.latest)
        self.assertNotIn('completed.json', remote.assets)
        release.publish(self.args)
        self.assertEqual(remote.latest, VERSION)
        self.assertEqual(remote.events.count('tap-push'), 1)
        self.assertIn('completed.json', remote.assets)

    def test_latest_ahead_missing_receipt_is_repairable(self):
        remote = self.publication()
        remote.latest = VERSION
        remote.tap_formula = (self.directory / 'tmux.rb').read_text()
        release.publish(self.args)
        self.assertIn('completed.json', remote.assets)
        self.assertNotIn('tap-push', remote.events)

    def test_fast_forward_tap_race_retries(self):
        remote = self.publication()
        remote.fail_pushes = 1
        release.publish(self.args)
        self.assertEqual(remote.pushes, 2)
        self.assertIn('completed.json', remote.assets)

    def test_tap_race_newer_formula_preserved(self):
        remote = self.publication()
        remote.fail_pushes = 1
        remote.newer_on_push_failure = True
        with self.assertRaisesRegex(ValueError, 'tap is newer'):
            release.publish(self.args)
        self.assertIn('2026.11.8.24.1', remote.tap_formula)
        self.assertNotIn('promote', remote.events)

    def test_tap_race_retry_limit(self):
        remote = self.publication()
        remote.fail_pushes = 4
        with self.assertRaisesRegex(ValueError, 'three publication attempts'):
            release.publish(self.args)
        self.assertEqual(remote.pushes, 3)
        self.assertNotIn('promote', remote.events)

    def test_retry_preserves_receipt_after_unrelated_tap_commit(self):
        remote = self.publication()
        release.publish(self.args)
        first_receipt = remote.assets['completed.json']
        # Another producer changes only a different formula. tmux bytes stay fixed.
        remote.tap_commit = 'f' * 40
        (self.directory / 'completed.json').unlink()
        release.publish(self.args)
        self.assertEqual(remote.assets['completed.json'], first_receipt)
        self.assertEqual(release.read(self.directory / 'completed.json')['tap_commit'], 'd' * 40)

    def test_equal_tap_version_with_different_formula_is_rejected(self):
        remote = self.publication()
        remote.tap_formula = (self.directory / 'tmux.rb').read_text() + '# SIMULATED wrong source\n'
        with self.assertRaisesRegex(ValueError, 'equal (remote )?tap version differs'):
            release.publish(self.args)
        self.assertNotIn('tap-push', remote.events)
        self.assertNotIn('promote', remote.events)

    def test_newer_latest_prevents_any_tap_mutation(self):
        remote = self.publication()
        remote.latest = '2026.11.8.24.1'
        with self.assertRaisesRegex(ValueError, 'latest is newer'):
            release.publish(self.args)
        self.assertNotIn('tap-push', remote.events)
        self.assertNotIn('promote', remote.events)

    def test_newer_remote_tap_after_push_prevents_latest(self):
        remote = self.publication()
        remote.newer_tap_after_push = True
        with self.assertRaisesRegex(ValueError, 'tap is newer'):
            release.publish(self.args)
        self.assertIn('tap-push', remote.events)
        self.assertIn('2026.11.8.24.1', remote.tap_formula)
        self.assertNotIn('promote', remote.events)
        self.assertNotIn('completed.json', remote.assets)

    def test_head_changes_after_tap_push_prevent_latest(self):
        remote = self.publication()
        remote.source_changes_after_push = True
        with self.assertRaisesRegex(ValueError, 'head changed'):
            release.publish(self.args)
        self.assertIn('tap-push', remote.events)
        self.assertNotIn('promote', remote.events)
        self.assertNotIn('completed.json', remote.assets)


if __name__ == '__main__':
    print('SIMULATED policy fixtures: inert payloads and local command transport; no external acceptance.', flush=True)
    unittest.main(verbosity=2)
