#!/usr/bin/env python3
"""Exercise package installation in a disposable matching distribution container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import tempfile
import time


def run(*args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    try:
        return subprocess.check_output(args, text=True, **kwargs).strip()
    except subprocess.CalledProcessError as error:
        print(error.output, flush=True)
        raise


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except subprocess.CalledProcessError:
            # A bound socket can exist before the server accepts commands.
            pass
        time.sleep(0.05)
    raise RuntimeError('timed out waiting for attached tmux client')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    package = args.package.resolve()
    source = args.source.resolve()
    require(os.geteuid() == 0 and Path('/.dockerenv').exists(),
            'verification requires root inside a disposable Docker container')
    distro, suite, architecture = args.target.split('-')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                   if '=' in line)
    require(release['ID'].strip('"') == distro and release['VERSION_CODENAME'].strip('"') == suite,
            'container distribution does not match target')
    require(run('dpkg', '--print-architecture') == architecture, 'container architecture mismatch')
    version = run('dpkg-deb', '-f', str(package), 'Version')
    require(run('dpkg-deb', '-f', str(package), 'Package') == 'tmux', 'wrong package')
    require(run('dpkg-deb', '-f', str(package), 'Architecture') == architecture, 'wrong architecture')
    require(version.endswith('-1+' + suite), 'wrong suite version')
    with tempfile.TemporaryDirectory(prefix='tmux-package-check-') as temporary:
        work = Path(temporary)
        unpacked = work / 'payload'
        run('dpkg-deb', '-R', str(package), str(unpacked))
        require(not any((unpacked / 'DEBIAN' / name).exists()
                        for name in ('preinst', 'postinst', 'prerm', 'postrm', 'triggers')),
                'personal package must have no maintainer hooks')
        identity = json.loads((unpacked / 'usr/share/tmux/personal-build.json').read_text())
        require(identity['target'] == args.target and identity['version'] + '-1+' + suite == version,
                'identity disagrees with package')
        expected_sha = ((source / '.personal-source-sha').read_text().strip()
                        if (source / '.personal-source-sha').exists()
                        else run('git', '-C', str(source), 'rev-parse', 'HEAD'))
        require(identity['source_sha'] == expected_sha, 'source identity mismatch')
        base = json.loads((source / 'packaging/personal/base.json').read_text())['upstream_sha']
        require(identity['upstream_sha'] == base, 'wrong upstream base')
        digest = hashlib.sha256((unpacked / 'usr/bin/tmux').read_bytes()).hexdigest()

        def installed():
            require(run('dpkg-query', '-W', '-f=${Version}', 'tmux') == version, 'installed version mismatch')
            require(hashlib.sha256(Path('/usr/bin/tmux').read_bytes()).hexdigest() == digest,
                    'installed executable differs from package')
            require(json.loads(Path('/usr/share/tmux/personal-build.json').read_text()) == identity,
                    'installed identity mismatch')
            require(shutil.which('tmux') == '/usr/bin/tmux', 'PATH shadows package')
            require('not found' not in run('ldd', '/usr/bin/tmux'), 'missing shared library')

        def install():
            run('apt-get', 'install', '-y', str(package))
            installed()

        run('apt-get', 'purge', '-y', 'tmux')
        install()
        print('PASS fresh package install', flush=True)
        run('apt-get', 'purge', '-y', 'tmux')
        run('apt-get', 'install', '-y', 'tmux')
        require(not Path('/usr/share/tmux/personal-build.json').exists(), 'upstream identity unexpected')

        def replace_preserving_server(action, label):
            socket = str(work / (label.replace(' ', '-') + '.sock'))
            master, slave = pty.openpty()
            environment = dict(os.environ, TERM='xterm-256color', TMUX_TMPDIR=str(work))
            environment.pop('TMUX', None)
            with os.fdopen(slave, 'rb+', buffering=0) as terminal:
                client = subprocess.Popen(['/usr/bin/tmux', '-S', socket, '-f', '/dev/null',
                                           'new-session', '-s', 'preserved', 'sleep 120'],
                                          stdin=terminal, stdout=terminal, stderr=terminal,
                                          env=environment, start_new_session=True)
                def tmux(*command):
                    return run('/usr/bin/tmux', '-S', socket, *command, env=environment)
                try:
                    wait_for(lambda: Path(socket).exists())
                    wait_for(lambda: bool(tmux('list-clients', '-F', '#{client_pid}')))
                    before = (tmux('display-message', '-p', '#{pid}'),
                              tmux('list-sessions', '-F', '#{session_id}:#{session_name}'),
                              tmux('list-clients', '-F', '#{client_pid}:#{client_tty}'))
                    action()
                    after = (tmux('display-message', '-p', '#{pid}'),
                             tmux('list-sessions', '-F', '#{session_id}:#{session_name}'),
                             tmux('list-clients', '-F', '#{client_pid}:#{client_tty}'))
                    require(before == after and client.poll() is None, 'live server or client changed')
                    os.kill(int(before[0]), 0)
                    print('PASS', label, 'preserved PID/session/attached client', before, flush=True)
                finally:
                    try:
                        if Path(socket).exists():
                            tmux('kill-server')
                    finally:
                        try:
                            client.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            client.terminate()
                            client.wait(timeout=10)
                        finally:
                            os.close(master)

        replace_preserving_server(install, 'upstream replacement')
        # First release has no earlier fork. Repack a lower-version predecessor to
        # exercise the actual dpkg upgrade and live-server replacement mechanics.
        previous = list(map(int, identity['version'].split('.')))
        for index in range(len(previous) - 1, -1, -1):
            if previous[index]:
                previous[index] -= 1
                break
        old_fork = '.'.join(map(str, previous))
        old_version = old_fork + '-1+' + suite
        run('dpkg', '--compare-versions', old_version, 'lt', version)
        control = unpacked / 'DEBIAN/control'
        control.write_text(control.read_text().replace('Version: ' + version, 'Version: ' + old_version))
        old_identity = dict(identity, version=old_fork)
        (unpacked / 'usr/share/tmux/personal-build.json').write_text(json.dumps(old_identity) + '\n')
        predecessor = work / 'predecessor.deb'
        run('dpkg-deb', '--root-owner-group', '--build', str(unpacked), str(predecessor))
        run('dpkg', '--force-downgrade', '-i', str(predecessor))
        replace_preserving_server(install, 'synthetic predecessor upgrade')
        for name, arguments in (('osc7501-root.sh', []),
                                ('osc7501-root-subscriptions.sh', [])):
            regression = source / 'regress' / name
            require(regression.exists(), 'missing required installed protocol regression: ' + name)
            print(run('sh', str(regression), *arguments, cwd=source / 'regress',
                      env=dict(os.environ, TEST_TMUX='/usr/bin/tmux', PYTHONDONTWRITEBYTECODE='1')),
                  flush=True)
        installed()
        print('PASS installed OSC7501 protocol regressions; binary sha256', digest, flush=True)


if __name__ == '__main__':
    main()
