#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sudo ./scripts/bootstrap.sh --release /path/genos.tar.gz --sha256 <sha256> --git-sha <40-hex-sha>
  sudo ./scripts/bootstrap.sh --release-url https://.../genos.tar.gz --sha256 <sha256> --git-sha <40-hex-sha>

The release archive is SHA-256 verified and safely extracted before any GenOS
release code is executed. Ubuntu 24.04 amd64 native is the certified MVP target.
EOF
}

RELEASE=""
RELEASE_URL=""
SHA256=""
GIT_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) RELEASE="${2:-}"; shift 2 ;;
    --release-url) RELEASE_URL="${2:-}"; shift 2 ;;
    --sha256) SHA256="${2:-}"; shift 2 ;;
    --git-sha) GIT_SHA="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$RELEASE" && -n "$RELEASE_URL" ]]; then
  echo "Choose exactly one of --release or --release-url" >&2
  exit 2
fi
if [[ -z "$RELEASE" && -z "$RELEASE_URL" ]]; then
  echo "A release archive or HTTPS release URL is required" >&2
  exit 2
fi
if [[ ! "$SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "--sha256 must be 64 hexadecimal characters" >&2
  exit 2
fi
if [[ ! "$GIT_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "--git-sha must be a 40-character hexadecimal commit id" >&2
  exit 2
fi
if [[ -n "$RELEASE_URL" && ! "$RELEASE_URL" =~ ^https:// ]]; then
  echo "--release-url must use HTTPS" >&2
  exit 2
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }

TMP_ROOT="$(mktemp -d -t genos-bootstrap.XXXXXXXX)"
chmod 700 "$TMP_ROOT"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

if [[ -n "$RELEASE_URL" ]]; then
  RELEASE="$TMP_ROOT/genos-release.tar.gz"
  python3 - "$RELEASE_URL" "$RELEASE" <<'PY'
from pathlib import Path
import sys
import urllib.request

url, output = sys.argv[1], Path(sys.argv[2])
request = urllib.request.Request(url, headers={"User-Agent": "GenOS-MVP-bootstrap/1"})
with urllib.request.urlopen(request, timeout=30) as response, output.open("wb") as handle:
    if response.status != 200:
        raise SystemExit(f"release download failed: HTTP {response.status}")
    total = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > 1024 * 1024 * 1024:
            raise SystemExit("release archive exceeds 1 GiB bootstrap limit")
        handle.write(chunk)
PY
fi

RELEASE="$(python3 - "$RELEASE" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]).expanduser().resolve()
if not p.is_file():
    raise SystemExit("release archive does not exist")
print(p)
PY
)"

EXTRACT="$TMP_ROOT/release"
mkdir -p "$EXTRACT"
python3 - "$RELEASE" "$SHA256" "$EXTRACT" <<'PY'
from pathlib import Path, PurePosixPath
import hashlib
import sys
import tarfile

archive = Path(sys.argv[1])
expected = sys.argv[2].lower()
destination = Path(sys.argv[3])

digest = hashlib.sha256()
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != expected:
    raise SystemExit(f"release checksum mismatch: expected {expected}, got {actual}")

with tarfile.open(archive, "r:*") as tf:
    members = tf.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsafe release member: {member.name}")
        if member.size < 0 or member.size > 1024 * 1024 * 1024:
            raise SystemExit(f"oversized release member: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsupported release member: {member.name}")
    tf.extractall(destination, filter="data")
if not (destination / "src" / "genos" / "__init__.py").is_file():
    raise SystemExit("release archive is missing src/genos package")
print(f"Verified release SHA-256: {actual}")
PY

RUN=(env "PYTHONPATH=$EXTRACT/src" python3 -m genos install --mode native --release "$RELEASE" --release-sha256 "$SHA256" --git-sha "$GIT_SHA" --json)
if [[ "$(id -u)" -eq 0 ]]; then
  "${RUN[@]}"
else
  command -v sudo >/dev/null || { echo "Run as root or install sudo" >&2; exit 2; }
  sudo "${RUN[@]}"
fi

echo "GenOS bootstrap completed. Open Mission Control on the installed host at http://127.0.0.1:17882/"
