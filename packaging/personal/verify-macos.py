#!/usr/bin/env python3
"""Exercise real Homebrew installation in a disposable macOS Actions runner."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import shutil
import struct
import subprocess
import tempfile
import termios
import time


def run(*args, **kwargs):
    print('+', *args, flush=True)
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def require(value, message):
    if not value:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--formula', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--bottle', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.uname().sysname == 'Darwin',
            'requires a disposable macOS Actions runner')
    require(args.formula == 'cosgroveb/tap/tmux' and args.target == 'macos-26-arm64', 'wrong target')
    run('brew', 'trust', '--formula', args.formula)
    formula_path = Path(output('brew', '--repository', 'cosgroveb/tap')) / 'Formula/tmux.rb'
    final_formula = formula_path.read_text()
    info = json.loads(output('brew', 'info', '--json=v2', args.formula))['formulae'][0]
    version = info['versions']['stable']
    base = json.loads((args.source / 'packaging/personal/base.json').read_text())['upstream_sha']
    configure = (args.source / 'configure.ac').read_text()
    match = re.search(r'^AC_INIT\(\[tmux\], ([^],)]+)\)', configure, re.M)
    require(match, 'cannot determine tmux version from configure.ac')
    upstream_version = match[1].strip()
    expected = {'source_sha': None, 'upstream_sha': base,
                'version': version, 'target': args.target}
    # The expected SHA comes independently from checkout/workflow identity, not the poured identity.
    expected['source_sha'] = os.environ['GITHUB_SHA']
    bottle = info['bottle']['stable']['files']['arm64_tahoe']
    require(hashlib.sha256(args.bottle.read_bytes()).hexdigest() == bottle['sha256'], 'bottle hash mismatch')
    require(Path(bottle['url']).name == args.bottle.name, 'bottle filename mismatch')
    selected = output('brew', 'ruby', '-e',
                      'f = Formulary.factory("cosgroveb/tap/tmux"); puts f.bottle.tag')
    require(selected == 'arm64_tahoe', 'Homebrew selected unsupported bottle: ' + selected)
    env = dict(os.environ)
    env.pop('TMUX', None)

    def installed():
        prefix = Path(output('brew', '--prefix', args.formula))
        identity = json.loads((prefix / 'share/tmux/personal-build.json').read_text())
        require(identity == expected, 'installed source identity mismatch')
        receipt = json.loads((prefix / 'INSTALL_RECEIPT.json').read_text())
        require(receipt['poured_from_bottle'] is True, 'Homebrew built from source')
        require(receipt['source']['tap'] == 'cosgroveb/tap', 'wrong installed tap')
        require(receipt['source']['versions']['stable'] == version, 'wrong installed version')
        binary = prefix / 'bin/tmux'
        require(output(str(binary), '-V') == 'tmux ' + upstream_version, 'unexpected tmux executable')
        require(shutil.which('tmux') is not None and
                Path(shutil.which('tmux')).resolve() == binary.resolve(), 'tmux PATH shadowing')
        run('brew', 'test', args.formula, env=env)
        return binary

    def install():
        run('brew', 'install', '--force-bottle', args.formula, env=env)
        installed()

    def preserve(binary, mutate, label):
        with tempfile.TemporaryDirectory(prefix='tmux-package-live-') as temporary:
            witness = Path(temporary) / 'tmux'
            shutil.copy2(binary, witness)
            socket = str(Path(temporary) / 'server')
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))

            def terminal():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            client = subprocess.Popen([str(binary), '-S', socket, '-f', '/dev/null',
                                       'new-session', '-s', 'personal-preserve'], stdin=slave,
                                      stdout=slave, stderr=slave, env=dict(env, TERM='xterm-256color'),
                                      preexec_fn=terminal)
            os.close(slave)

            def snapshot():
                return tuple(output(str(witness), '-S', socket, *command) for command in (
                    ('display-message', '-p', '#{pid}'),
                    ('list-sessions', '-F', '#{session_id}:#{session_name}'),
                    ('list-clients', '-F', '#{client_pid}:#{client_tty}:#{session_id}')))

            try:
                deadline = time.monotonic() + 10
                while True:
                    if Path(socket).exists():
                        try:
                            before = snapshot()
                        except subprocess.CalledProcessError:
                            before = None
                        if before and before[2]:
                            break
                    require(client.poll() is None and time.monotonic() < deadline,
                            'attached private server did not start')
                    time.sleep(0.05)
                mutate()
                require(snapshot() == before, label + ' changed server PID/sessions/attached clients')
                require(client.poll() is None, label + ' detached client')
                print('PASS', label, 'preserved PID/sessions/attached clients', before, flush=True)
            finally:
                try:
                    if Path(socket).exists():
                        run(str(witness), '-S', socket, 'kill-server')
                finally:
                    try:
                        client.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        client.terminate()
                        client.wait(timeout=10)
                    finally:
                        os.close(master)

    install()
    print('PASS fresh bottle installation', flush=True)
    run('brew', 'uninstall', '--formula', args.formula)
    run('brew', 'install', '--formula', 'homebrew/core/tmux')
    upstream = Path(output('brew', '--prefix', 'homebrew/core/tmux')) / 'bin/tmux'
    upstream_receipt = json.loads((upstream.parent.parent / 'INSTALL_RECEIPT.json').read_text())
    require(upstream_receipt['source']['tap'] == 'homebrew/core', 'replacement baseline is not core tmux')

    def replace_core():
        run('brew', 'uninstall', '--formula', 'homebrew/core/tmux')
        install()

    preserve(upstream, replace_core, 'core replacement')
    run('brew', 'uninstall', '--formula', args.formula)
    # First publication has no historical fork. Build an actual predecessor formula
    # from this source with an earlier package version to exercise Homebrew upgrade.
    predecessor = '0.0.0.0.1'
    source_hash = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    template = (args.source / 'packaging/personal/tmux.rb.in').read_text()
    for key, value in {'VERSION': predecessor, 'SOURCE_SHA': expected['source_sha'],
                       'UPSTREAM_SHA': base, 'UPSTREAM_VERSION': upstream_version,
                       'SOURCE_SHA256': source_hash}.items():
        template = template.replace('@' + key + '@', value)
    try:
        formula_path.write_text(template)
        run('brew', 'trust', '--formula', args.formula)
        cache = Path(output('brew', '--cache', '--build-from-source', args.formula))
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.archive, cache)
        run('brew', 'install', '--build-from-source', args.formula)
        old_binary = Path(output('brew', '--prefix', args.formula)) / 'bin/tmux'
        old_identity = json.loads((old_binary.parent.parent / 'share/tmux/personal-build.json').read_text())
        require(old_identity['version'] == predecessor, 'predecessor build missing')
        formula_path.write_text(final_formula)

        def upgrade():
            run('brew', 'upgrade', '--force-bottle', args.formula, env=env)
            installed()

        preserve(old_binary, upgrade, 'synthetic predecessor upgrade')
    finally:
        formula_path.write_text(final_formula)
    binary = installed()
    regress_env = dict(env, TEST_TMUX=str(binary))
    regress = args.source / 'regress'
    scripts = sorted(regress.glob('osc7501-root*.sh'))
    require(len(scripts) == 2, 'missing installed OSC7501 root regressions')
    for script in scripts:
        require(script.exists(), 'missing installed protocol regression ' + str(script))
        run('sh', str(script), cwd=args.source / 'regress', env=regress_env)
    forwarding = regress / 'program-status.sh'
    require(forwarding.exists(), 'missing installed forwarding regression program-status.sh')
    run('sh', str(forwarding), cwd=args.source / 'regress', env=regress_env)
    print('PASS real poured-bottle identity and installed OSC7501 regressions', flush=True)


if __name__ == '__main__':
    main()
