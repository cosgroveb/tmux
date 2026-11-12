#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
    echo "usage: $0 VERSION SOURCE_SHA TARGET OUTPUT_DIR" >&2
    exit 2
fi
version=$1
source_sha=$2
target=$3
output=$(mkdir -p "$4" && cd "$4" && pwd)
source=$(pwd)
case "$target" in
    debian-bookworm-*|debian-trixie-*|ubuntu-noble-*) ;;
    *) echo "unsupported target: $target" >&2; exit 1 ;;
esac
suite=${target#*-}; suite=${suite%-*}
architecture=${target##*-}
. /etc/os-release
test "$ID-$VERSION_CODENAME" = "${target%-*}"
test "$(dpkg --print-architecture)" = "$architecture"
if [ -f .personal-source-sha ]; then
    test "$(cat .personal-source-sha)" = "$source_sha"
else
    test "$(git rev-parse HEAD)" = "$source_sha"
    test -z "$(git status --porcelain --untracked-files=no)"
fi
python3 - "$version" "$source_sha" <<'PY'
import re, sys
assert re.fullmatch(r'[0-9]+(?:\.[0-9]+){4}', sys.argv[1]), 'invalid version'
assert re.fullmatch(r'[0-9a-f]{40}', sys.argv[2]), 'invalid source SHA'
PY
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir "$work/source" "$work/root"
tar --exclude=.git --exclude=packaging/personal/__pycache__ -cf - . | tar -xf - -C "$work/source"
cd "$work/source"
if [ -f Makefile ]; then
    make distclean
fi
sh autogen.sh
./configure --prefix=/usr --enable-utf8proc
make -j"$(getconf _NPROCESSORS_ONLN)"
make DESTDIR="$work/root" install
install -d "$work/root/usr/share/doc/tmux" "$work/root/usr/share/tmux" "$work/root/DEBIAN" debian
install -m644 COPYING "$work/root/usr/share/doc/tmux/copyright"
python3 - "$work/root/usr/share/tmux/personal-build.json" "$version" "$source_sha" "$target" <<'PY'
import json, pathlib, sys
base = json.loads(pathlib.Path('packaging/personal/base.json').read_text())['upstream_sha']
with open(sys.argv[1], 'w') as f:
    json.dump(dict(version=sys.argv[2], source_sha=sys.argv[3], target=sys.argv[4],
                   upstream_sha=base), f, sort_keys=True)
    f.write('\n')
PY
cat >debian/control <<'EOF'
Source: tmux
Section: admin
Priority: optional
Maintainer: Brian Cosgrove <cosgroveb@users.noreply.github.com>

Package: tmux
Architecture: any
Description: terminal multiplexer with personal OSC7501 patches
EOF
dependencies=$(dpkg-shlibdeps -O -e"$work/root/usr/bin/tmux" | sed -n 's/^shlibs:Depends=//p')
test -n "$dependencies"
cat >"$work/root/DEBIAN/control" <<EOF
Package: tmux
Version: $version-1+$suite
Architecture: $architecture
Section: admin
Priority: optional
Maintainer: Brian Cosgrove <cosgroveb@users.noreply.github.com>
Depends: $dependencies
Description: terminal multiplexer with personal OSC7501 patches
EOF
package="$output/tmux_${version}-1+${suite}_${architecture}.deb"
dpkg-deb --root-owner-group --build "$work/root" "$package"
python3 "$source/packaging/personal/verify-install.py" --package "$package" --source "$source" --target "$target"
python3 - "$package" "$target" "$version" "$suite" "$architecture" "$output" <<'PY'
import hashlib, json, pathlib, sys
package, target, version, suite, architecture, output = sys.argv[1:]
digest = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
record = dict(asset=pathlib.Path(package).name, sha256=digest(package), package='tmux',
              version=version + '-1+' + suite, suite=suite, architecture=architecture,
              binary_sha256=digest('/usr/bin/tmux'), verified=True)
pathlib.Path(output, target + '.json').write_text(json.dumps(record, sort_keys=True) + '\n')
PY
