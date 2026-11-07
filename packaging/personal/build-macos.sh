#!/bin/bash
# Run only on a disposable native macOS GitHub Actions runner.
set -euo pipefail
[ "$#" -eq 4 ] || { echo "usage: $0 VERSION SOURCE_SHA SOURCE_ARCHIVE OUTPUT_DIR" >&2; exit 2; }
version=$1
source_sha=$2
archive=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")
mkdir -p "$4"
output=$(cd "$4" && pwd)
source_dir=$(pwd)
if [ "${GITHUB_ACTIONS:-}" != true ] || [ "$(uname -m)" != arm64 ] || [ "$(sw_vers -productVersion | cut -d. -f1)" != 26 ]; then
    echo 'macOS bottle build requires a disposable native macOS 26 Actions runner' >&2
    exit 1
fi
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
export HOMEBREW_NO_BOTTLE_SOURCE_FALLBACK=1
formula=cosgroveb/tap/tmux
root_url="https://github.com/cosgroveb/tmux/releases/download/personal-$version"
brew tap-new --no-git cosgroveb/tap
formula_path="$(brew --repository cosgroveb/tap)/Formula/tmux.rb"
python3 - "$source_dir/packaging/personal/tmux.rb.in" "$formula_path" "$version" "$source_sha" "$archive" <<'PY'
import hashlib, pathlib, re, sys
src, dest, version, sha, archive = sys.argv[1:]
assert re.fullmatch(r'[1-9][0-9]*(?:\.(?:0|[1-9][0-9]*)){4}', version)
assert re.fullmatch(r'[0-9a-f]{40}', sha)
text = pathlib.Path(src).read_text()
for key, value in {'VERSION': version, 'SOURCE_SHA': sha,
                   'SOURCE_SHA256': hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest()}.items():
    text = text.replace('@' + key + '@', value)
pathlib.Path(dest).write_text(text)
PY
brew trust --formula "$formula"
source_cache=$(brew --cache --build-from-source "$formula")
mkdir -p "$(dirname "$source_cache")"
cp "$archive" "$source_cache"
# A core tmux on the runner must not conceal the personal formula.
if brew list --formula homebrew/core/tmux >/dev/null 2>&1; then
    brew uninstall --formula homebrew/core/tmux
fi
brew install --build-bottle "$formula"
brew test "$formula"
cd "$output"
brew bottle --json --no-rebuild --root-url "$root_url" "$formula"
python3 - "$version" <<'PY'
import hashlib, json, pathlib, sys
files = list(pathlib.Path('.').glob('*.bottle.json'))
assert len(files) == 1, files
entry = json.loads(files[0].read_text())['cosgroveb/tap/tmux']
assert entry['formula']['pkg_version'] == sys.argv[1]
assert set(entry['bottle']['tags']) == {'arm64_tahoe'}
tag = entry['bottle']['tags']['arm64_tahoe']
local, remote = pathlib.Path(tag['local_filename']), pathlib.Path(tag['filename'])
assert remote.name == str(remote) and remote.suffixes[-2:] == ['.tar', '.gz']
assert hashlib.sha256(local.read_bytes()).hexdigest() == tag['sha256']
if local != remote:
    local.rename(remote)
pathlib.Path('bottle-metadata.json').write_text(json.dumps(entry, indent=2) + '\n')
PY
# Merge the real metadata; preserve Homebrew's relocation and cellar decisions.
brew bottle --merge --write --no-commit ./*.bottle.json
brew audit "$formula"
brew info --json=v2 "$formula" > formula-info.json
python3 - "$root_url" "$version" <<'PY'
import json, pathlib, sys, urllib.parse
entry = json.loads(pathlib.Path('bottle-metadata.json').read_text())
tag = entry['bottle']['tags']['arm64_tahoe']
info = json.loads(pathlib.Path('formula-info.json').read_text())['formulae'][0]
assert info['full_name'] == 'cosgroveb/tap/tmux'
assert info['versions']['stable'] == sys.argv[2]
assert info['urls']['stable']['url'] == sys.argv[1] + '/tmux-' + sys.argv[2] + '.tar.gz'
file = info['bottle']['stable']['files']['arm64_tahoe']
assert file['sha256'] == tag['sha256']
assert file['url'] == sys.argv[1] + '/' + tag['filename']
assert pathlib.PurePosixPath(urllib.parse.urlparse(file['url']).path).name == tag['filename']
PY
cp "$formula_path" "$output/tmux.rb"
bottle_cache=$(brew --cache --force-bottle "$formula")
mkdir -p "$(dirname "$bottle_cache")"
bottle=$(python3 -c 'import json; print(json.load(open("bottle-metadata.json"))["bottle"]["tags"]["arm64_tahoe"]["filename"])')
cp "$bottle" "$bottle_cache"
brew uninstall --formula "$formula"
python3 "$source_dir/packaging/personal/verify-macos.py" \
    --formula "$formula" --source "$source_dir" --target macos-26-arm64 --bottle "$output/$bottle" --archive "$archive"
python3 - "$version" <<'PY'
import json, pathlib, sys
entry = json.loads(pathlib.Path('bottle-metadata.json').read_text())
tag = entry['bottle']['tags']['arm64_tahoe']
record = dict(asset=tag['filename'], sha256=tag['sha256'], formula='cosgroveb/tap/tmux',
              version=sys.argv[1], bottle_tag='arm64_tahoe', verified=True)
pathlib.Path('macos-26-arm64.json').write_text(json.dumps(record, indent=2) + '\n')
PY
