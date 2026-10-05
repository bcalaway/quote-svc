#!/usr/bin/env bash
# Runs the test suite where PyPI isn't reachable (Claude's sandbox), by
# cloning the pure-Python dependencies from GitHub at their pinned versions.
# With working pip, just `pip install -r requirements.txt -r requirements-dev.txt`
# and run `pytest`, or use `docker build --target test .` like CI.
#
# Usage: scripts/sandbox-test.sh [pytest args...]   (default: tests/)
# Clones once into $DEPS (default ${TMPDIR:-/tmp}/quote-svc-deps); re-runs reuse it.
# Packages already importable (and the compiled ones: pydantic, psycopg2,
# grpcio) aren't cloned. test_grpc.py is skipped: it needs compiled grpcio,
# and app.grpc_server is stubbed out so app.main imports without it.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPS=${DEPS:-${TMPDIR:-/tmp}/quote-svc-deps}
mkdir -p "$DEPS/stubs"

pin() {  # pin <package> -> its pinned version from requirements*.txt
  sed -n "s/^$1\(\[[a-z]*\]\)\?==\([0-9.]*\).*/\2/Ip" "$ROOT"/requirements*.txt | head -1
}

# import name | GitHub repo | tag (VER = pinned version) | path to add | default version
DEPS_TABLE="
sqlalchemy|sqlalchemy/sqlalchemy|rel_VER_|lib|$(pin sqlalchemy)
alembic|sqlalchemy/alembic|rel_VER_|.|$(pin alembic)
mako|sqlalchemy/mako|rel_VER_|.|1.3.10
markupsafe|pallets/markupsafe|VER|src|3.0.2
fastapi|fastapi/fastapi|VER|.|$(pin fastapi)
httpx|encode/httpx|VER|.|$(pin httpx)
httpx2|pydantic/httpx2|vVER|src/httpx2|$(pin httpx2)
httpcore2|pydantic/httpx2|vVER|src/httpcore2|$(pin httpx2)
truststore|sethmlarson/truststore|vVER|src|0.10.4
httpcore|encode/httpcore|VER|.|1.0.9
authlib|authlib/authlib|vVER|.|$(pin authlib)
joserfc|authlib/joserfc|VER|src|1.7.5
pytest|pytest-dev/pytest|VER|src|$(pin pytest)
pluggy|pytest-dev/pluggy|VER|src|1.6.0
iniconfig|pytest-dev/iniconfig|vVER|src|2.1.0
"
PYPATH="$DEPS/stubs"
while IFS='|' read -r mod repo tagfmt sub ver; do
  [ -n "$mod" ] || continue
  if python3 -c "import $mod" 2>/dev/null; then continue; fi
  tag=${tagfmt//VER/$ver}; [[ "$tagfmt" == rel_VER_ ]] && tag="rel_${ver//./_}"
  dir="$DEPS/${repo##*/}"
  if [ ! -d "$dir" ]; then
    echo "cloning $repo @ $tag" >&2
    git clone -q --depth 1 --branch "$tag" "https://github.com/$repo" "$dir" 2>/dev/null \
      || { echo "can't clone $repo at $tag; install $mod another way" >&2; exit 1; }
  fi
  # setuptools-scm packages need a _version.py that only a build writes.
  for pkg in "$dir/src/_pytest" "$dir/src/pluggy" "$dir/src/iniconfig"; do
    [ -d "$pkg" ] && [ ! -f "$pkg/_version.py" ] && printf '__version__ = version = "%s"\nversion_tuple = (%s)\n' "$ver" "${ver//./, }" > "$pkg/_version.py"
  done
  PYPATH="$PYPATH:$dir/$sub"
done <<< "$DEPS_TABLE"

# Stubs: fastapi's annotated_doc marker, and the gRPC server (compiled grpcio).
mkdir -p "$DEPS/stubs/annotated_doc"
python3 -c "import annotated_doc" 2>/dev/null || printf 'class Doc:\n    def __init__(self, documentation):\n        self.documentation = documentation\n' > "$DEPS/stubs/annotated_doc/__init__.py"
cat > "$DEPS/stubs/sandbox_grpc_stub.py" <<'PY'
import sys, types
try:
    import grpc  # noqa: F401
except ImportError:
    stub = types.ModuleType("app.grpc_server")
    async def start_grpc_server(port):
        raise RuntimeError("gRPC isn't available in this sandbox")
    stub.start_grpc_server = start_grpc_server
    sys.modules["app.grpc_server"] = stub
PY

cd "$ROOT"
[ $# -gt 0 ] || set -- tests/
PYTHONPATH="$PYPATH:${PYTHONPATH:-}" exec python3 -m pytest -q -p no:cacheprovider -p sandbox_grpc_stub \
  --ignore=tests/test_grpc.py "$@"
