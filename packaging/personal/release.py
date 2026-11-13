#!/usr/bin/env python3
"""Build identity and immutable assets for the personal tmux distribution."""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tarfile

REPO = 'cosgroveb/tmux'
TAP = 'cosgroveb/homebrew-tap'
# Upstream commit this personal branch is cut from; the tmux version itself
# comes from AC_INIT in configure.ac of the selected source.
BASE = json.loads((Path(__file__).with_name('base.json')).read_text())['upstream_sha']
TARGETS = {f'{osname}-{suite}-{arch}' for osname, suite in
           [('debian', 'bookworm'), ('debian', 'trixie'), ('ubuntu', 'noble')]
           for arch in ('amd64', 'arm64')} | {'macos-26-arm64'}


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def gh(*args):
    return run('gh', *args)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs)


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*(?:\.(?:0|[1-9][0-9]*)){4}', value), 'invalid fork version')
    return tuple(map(int, value.split('.')))


def upstream_version(configure):
    match = re.search(r'^AC_INIT\(\[tmux\], ([^],)]+)\)', configure, re.M)
    require(match, 'cannot determine tmux version from configure.ac')
    return match[1].strip()


def hexvalue(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value)


def asset(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+\-]*', value), 'unsafe asset name')
    return value


def validate(directory):
    directory = Path(directory)
    m = read(directory / 'manifest.json')
    require(type(m['schema']) is int and m['schema'] == 1, 'unknown schema')
    version(m['version'])
    require(m['tag'] == 'personal-' + m['version'], 'tag/version mismatch')
    require(hexvalue(m['source_sha'], 40) and m['upstream_sha'] == BASE, 'source identity mismatch')
    w = m['workflow']
    require(all(isinstance(w[k], str) and re.fullmatch(r'[1-9][0-9]*', w[k]) for k in ('run_id', 'run_number', 'run_attempt')), 'invalid workflow identity')
    date = datetime.datetime.fromisoformat(w['created_at'].replace('Z', '+00:00'))
    require(date.utcoffset() == datetime.timedelta(0), 'workflow timestamp must be UTC')
    expected = f'{date.year}.{date.month}.{date.day}.{w["run_number"]}.{w["run_attempt"]}'
    require(m['version'] == expected, 'workflow/version mismatch')
    require(set(m['targets']) == TARGETS, 'incomplete or unknown target matrix')
    require(m['source']['asset'] == f'tmux-{m["version"]}.tar.gz', 'source filename mismatch')
    records = [m['source']]
    for target, record in m['targets'].items():
        require(record.get('verified') is True, f'unverified target: {target}')
        if target == 'macos-26-arm64':
            require(record['formula'] == 'cosgroveb/tap/tmux' and record['version'] == m['version'] and record['bottle_tag'] == 'arm64_tahoe', 'bottle metadata mismatch')
        else:
            _, suite, arch = target.split('-')
            debversion = m['version'] + '-1+' + suite
            require(record['package'] == 'tmux' and record['suite'] == suite and record['architecture'] == arch and record['version'] == debversion, 'package metadata mismatch')
            require(record['asset'] == f'tmux_{debversion}_{arch}.deb' and hexvalue(record['binary_sha256'], 64), 'package filename/hash mismatch')
        records.append(record)
    names = [asset(r['asset']) for r in records]
    require(len(set(names)) == len(names), 'duplicate asset')
    for record in records:
        require(hexvalue(record['sha256'], 64) and digest(directory / record['asset']) == record['sha256'], 'payload checksum mismatch')
    with tarfile.open(directory / m['source']['asset'], 'r:gz') as archive:
        marker = archive.extractfile(f'tmux-{m["version"]}/.personal-source-sha')
        require(marker is not None and marker.read() == (m['source_sha'] + '\n').encode(), 'source archive identity mismatch')
        configure = archive.extractfile(f'tmux-{m["version"]}/configure.ac')
        require(configure is not None, 'source archive missing configure.ac')
        require(m['upstream_version'] == upstream_version(configure.read().decode()), 'source identity mismatch')
    lines = (directory / 'SHA256SUMS').read_text().splitlines()
    expected_sums = {name: digest(directory / name) for name in names + ['manifest.json']}
    actual = {}
    for line in lines:
        sha, name = line.split('  ', 1)
        require(name not in actual, 'duplicate checksum')
        actual[name] = sha
    require(actual == expected_sums, 'checksum inventory mismatch')
    receipt = directory / 'completed.json'
    if receipt.exists():
        r = read(receipt)
        require(r['schema'] == 1 and r['tag'] == m['tag'] and r['source_sha'] == m['source_sha'] and r['manifest_sha256'] == digest(directory / 'manifest.json') and hexvalue(r['tap_commit'], 40), 'completion receipt mismatch')
    return m


def identity(args):
    w = json.loads(gh('api', f'repos/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'))
    date = datetime.datetime.fromisoformat(w['created_at'].replace('Z', '+00:00'))
    v = f'{date.year}.{date.month}.{date.day}.{w["run_number"]}.{os.environ["GITHUB_RUN_ATTEMPT"]}'
    m = dict(schema=1, version=v, tag='personal-' + v, source_sha=os.environ['GITHUB_SHA'], upstream_sha=BASE, upstream_version=upstream_version((Path(__file__).resolve().parents[2] / 'configure.ac').read_text()), workflow=dict(run_id=str(w['id']), run_number=str(w['run_number']), run_attempt=os.environ['GITHUB_RUN_ATTEMPT'], created_at=w['created_at']))
    write(args.output, m)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'version={v}\ntag={m["tag"]}\n')


def assemble(args):
    directory = Path(args.artifacts)
    m = read(args.identity)
    m['source'] = dict(asset=f'tmux-{m["version"]}.tar.gz')
    m['source']['sha256'] = digest(directory / m['source']['asset'])
    m['targets'] = {}
    for target in sorted(TARGETS):
        path = directory / (target + '.json')
        require(path.is_file(), 'missing target record: ' + target)
        m['targets'][target] = read(path)
    write(directory / 'manifest.json', m)
    names = [m['source']['asset']] + [r['asset'] for r in m['targets'].values()] + ['manifest.json']
    (directory / 'SHA256SUMS').write_text(''.join(f'{digest(directory / name)}  {name}\n' for name in sorted(names)))
    validate(directory)


def verify_formula(path, m):
    text = path.read_text()
    root = f'https://github.com/{REPO}/releases/download/{m["tag"]}'
    source = m['source']
    bottle = m['targets']['macos-26-arm64']
    require(formula_version(text) == m['version'], 'formula version mismatch')
    require(f'  url "{root}/{source["asset"]}"' in text and f'  sha256 "{source["sha256"]}"' in text, 'formula source mismatch')
    require(f'root_url "{root}"' in text, 'formula bottle root mismatch')
    require(re.search(r'sha256[^\n]*arm64_tahoe: "' + bottle['sha256'] + '"', text), 'formula bottle hash mismatch')
    require(m['source_sha'] in text and BASE in text, 'formula source identity mismatch')


def release_state(tag):
    result = subprocess.run(['gh', 'api', f'repos/{REPO}/releases/tags/{tag}'], text=True, capture_output=True)
    if result.returncode:
        require('HTTP 404' in result.stderr, result.stderr)
        return None
    return json.loads(result.stdout)


def immutable_upload(directory, state, name):
    path = Path(directory) / name
    existing = next((a for a in state['assets'] if a['name'] == name), None)
    if existing:
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(['gh', 'release', 'download', state['tag_name'], '--repo', REPO, '--pattern', name, '--dir', temp], check=True)
            require(digest(Path(temp) / name) == digest(path), 'immutable asset differs: ' + name)
    else:
        gh('release', 'upload', state['tag_name'], str(path), '--repo', REPO)


def stage(args):
    m = validate(args.artifacts)
    verify_formula(Path(args.artifacts) / 'tmux.rb', m)
    state = release_state(m['tag'])
    if state is None:
        gh('release', 'create', m['tag'], '--repo', REPO, '--target', m['source_sha'], '--title', 'Personal tmux ' + m['version'], '--notes', 'Verified personal tmux packages.', '--latest=false')
        state = release_state(m['tag'])
    require(state['target_commitish'] == m['source_sha'], 'release source differs')
    resolved = gh('api', f'repos/{REPO}/commits/{m["tag"]}', '--jq', '.sha')
    require(resolved == m['source_sha'], 'release tag source differs')
    names = [m['source']['asset'], 'manifest.json', 'SHA256SUMS'] + [r['asset'] for r in m['targets'].values()]
    for name in names:
        immutable_upload(args.artifacts, state, name)
    immutable_upload(args.artifacts, state, 'tmux.rb')


def eligible(m, head, latest, tap_version):
    require(head == m['source_sha'], 'distribution head changed')
    for label, candidate in [('latest', latest), ('tap', tap_version)]:
        if candidate:
            require(version(candidate) <= version(m['version']), label + ' is newer')


def latest_version():
    result = subprocess.run(['gh', 'api', f'repos/{REPO}/releases/latest'], text=True, capture_output=True)
    if result.returncode:
        require('HTTP 404' in result.stderr, result.stderr)
        return None
    tag = json.loads(result.stdout)['tag_name']
    require(tag.startswith('personal-'), 'latest release has unknown identity')
    return tag.removeprefix('personal-')


def formula_version(text):
    if text is None:
        return None
    match = re.search(r'^  url "https://github\.com/cosgroveb/tmux/releases/download/'
                      r'personal-([^/"\n]+)/tmux-\1\.tar\.gz"$', text, re.M)
    require(match, 'tap formula has no matching fork release URL')
    version(match[1])
    require(all(value == match[1] for value in re.findall(r'^  version "([^"]+)"$', text, re.M)),
            'explicit formula version differs from release URL')
    return match[1]


def tap_version(path):
    return formula_version(path.read_text() if path.exists() else None)


def remote_tap_formula():
    result = subprocess.run(['gh', 'api', f'repos/{TAP}/contents/Formula/tmux.rb'], text=True, capture_output=True)
    if result.returncode:
        require('HTTP 404' in result.stderr, result.stderr)
        return None
    return base64.b64decode(json.loads(result.stdout)['content']).decode()


def publish(args):
    require(bool(os.environ.get('HOMEBREW_TAP_TOKEN', '').strip()),
            'HOMEBREW_TAP_TOKEN is required before staging or publication')
    m = validate(args.artifacts)
    directory = Path(args.artifacts).resolve()
    # Recheck immutable remote payloads even on publication-only retries.
    stage(args)
    state = release_state(m['tag'])
    if any(a['name'] == 'completed.json' for a in state['assets']):
        with tempfile.TemporaryDirectory() as receipt_dir:
            subprocess.run(['gh', 'release', 'download', m['tag'], '--repo', REPO, '--pattern', 'completed.json', '--dir', receipt_dir], check=True)
            receipt = read(Path(receipt_dir) / 'completed.json')
            local = directory / 'completed.json'
            if local.exists():
                require(read(local) == receipt, 'remote receipt differs')
            write(local, receipt)
            validate(directory)
    with tempfile.TemporaryDirectory() as temp:
        checkout = Path(temp) / 'tap'
        subprocess.run(['gh', 'repo', 'clone', TAP, str(checkout)], check=True)
        run('git', '-C', str(checkout), 'config', 'credential.helper', '')
        run('git', '-C', str(checkout), 'config', '--add', 'credential.helper', '!gh auth git-credential')
        formula = checkout / 'Formula/tmux.rb'
        subprocess.run(['brew', 'tap', 'cosgroveb/tap'], check=True)
        subprocess.run(['brew', 'trust', '--formula', 'cosgroveb/tap/tmux'], check=True)
        audit_formula = Path(run('brew', '--repository', 'cosgroveb/tap')) / 'Formula/tmux.rb'
        def check():
            head = gh('api', f'repos/{REPO}/git/ref/heads/osc7501-direct-personal-20261113', '--jq', '.object.sha')
            remote_formula = remote_tap_formula()
            remote_version = formula_version(remote_formula)
            eligible(m, head, latest_version(), remote_version)
            if remote_version == m['version']:
                require(remote_formula == (directory / 'tmux.rb').read_text(), 'equal remote tap version differs')
            # Use both package managers' actual ordering, not lexical ordering.
            for old in filter(None, [latest_version(), remote_version]):
                subprocess.run(['dpkg', '--compare-versions', old, 'le', m['version']], check=True)
                require(run('brew', 'ruby', '-e', 'require "version"; puts(Version.new(ARGV[0]) <= Version.new(ARGV[1]))', old, m['version']) == 'true', 'Homebrew version order rejected publication')
        for attempt in range(3):
            run('git', '-C', str(checkout), 'fetch', 'origin')
            run('git', '-C', str(checkout), 'reset', '--hard', 'origin/HEAD')
            check()
            if tap_version(formula) == m['version']:
                require(formula.read_bytes() == (directory / 'tmux.rb').read_bytes(), 'equal tap version differs')
            formula.parent.mkdir(exist_ok=True)
            formula.write_bytes((directory / 'tmux.rb').read_bytes())
            audit_formula.parent.mkdir(exist_ok=True)
            audit_formula.write_bytes(formula.read_bytes())
            subprocess.run(['brew', 'audit', '--strict', 'cosgroveb/tap/tmux'], check=True)
            run('git', '-C', str(checkout), 'add', 'Formula/tmux.rb')
            if subprocess.run(['git', '-C', str(checkout), 'diff', '--cached', '--quiet']).returncode:
                run('git', '-C', str(checkout), '-c', 'user.name=tmux release', '-c', 'user.email=actions@users.noreply.github.com', 'commit', '-m', 'Update personal tmux ' + m['version'])
                check()
                if subprocess.run(['git', '-C', str(checkout), 'push', 'origin', 'HEAD'], env={**os.environ, 'GH_TOKEN': os.environ['HOMEBREW_TAP_TOKEN']}).returncode:
                    continue
            break
        else:
            raise ValueError('tap push failed after three publication attempts')
        commit = run('git', '-C', str(checkout), 'rev-parse', 'HEAD')
        remote = gh('api', f'repos/{TAP}/contents/Formula/tmux.rb?ref={commit}', '--jq', '.content')
        require(base64.b64decode(remote).decode() == (directory / 'tmux.rb').read_text(), 'tap readback mismatch')
        check()
        gh('release', 'edit', m['tag'], '--repo', REPO, '--latest')
        require(latest_version() == m['version'], 'latest readback mismatch')
        check()
        receipt_path = directory / 'completed.json'
        receipt = dict(schema=1, tag=m['tag'], source_sha=m['source_sha'], manifest_sha256=digest(directory / 'manifest.json'), tap_commit=commit)
        if receipt_path.exists():
            receipt = read(receipt_path)
            previous = gh('api', f'repos/{TAP}/contents/Formula/tmux.rb?ref={receipt["tap_commit"]}', '--jq', '.content')
            require(base64.b64decode(previous).decode() == (directory / 'tmux.rb').read_text(), 'completed tap formula differs')
        write(receipt_path, receipt)
        immutable_upload(directory, release_state(m['tag']), 'completed.json')
        # A fresh inventory forces a checksum readback after the upload.
        immutable_upload(directory, release_state(m['tag']), 'completed.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('identity'); p.add_argument('--output', required=True)
    p = sub.add_parser('assemble'); p.add_argument('--identity', required=True); p.add_argument('--artifacts', required=True)
    for command in ('validate', 'stage', 'publish'):
        p = sub.add_parser(command); p.add_argument('--artifacts', required=True)
    args = parser.parse_args()
    if args.command == 'validate':
        validate(args.artifacts)
    else:
        globals()[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
