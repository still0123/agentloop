#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script requires macOS." >&2
  exit 1
fi
if [[ -n "${PYTHON:-}" ]]; then
  python_bin="$PYTHON"
elif [[ -x "$repo_root/.venv/bin/python" ]]; then
  python_bin="$repo_root/.venv/bin/python"
else
  python_bin="python3"
fi

"$python_bin" -c \
  'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo "AgentLoop requires Python 3.10 or newer." >&2
  exit 1
}
"$python_bin" -c 'import PyInstaller, webview' 2>/dev/null || {
  echo "Install desktop build dependencies first:" >&2
  echo "  $python_bin -m pip install -e '.[desktop]'" >&2
  exit 1
}

cd "$repo_root"
PYINSTALLER_CONFIG_DIR="$repo_root/build/pyinstaller-cache" \
  "$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name AgentLoop \
  --specpath "$repo_root/build" \
  --workpath "$repo_root/build/pyinstaller" \
  --distpath "$repo_root/dist" \
  --osx-bundle-identifier io.github.still0123.agentloop \
  --add-data "$repo_root/agentloop/web_ui.html:agentloop" \
  --collect-all webview \
  --exclude-module pytest \
  --exclude-module ruff \
  scripts/macos_desktop_app.py

app="$repo_root/dist/AgentLoop.app"
mv "$app/Contents/MacOS/AgentLoop" "$app/Contents/MacOS/AgentLoop.bin"
install -m 755 \
  "$repo_root/scripts/macos_app_launcher.sh" \
  "$app/Contents/MacOS/AgentLoop"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
"$python_bin" - "$app/Contents/Info.plist" "$version" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
with path.open("rb") as handle:
    info = plistlib.load(handle)
info.update(
    {
        "CFBundleDisplayName": "AgentLoop",
        "CFBundleExecutable": "AgentLoop",
        "CFBundleIdentifier": "io.github.still0123.agentloop",
        "CFBundleShortVersionString": sys.argv[2],
        "CFBundleVersion": sys.argv[2],
        "LSMinimumSystemVersion": "12.0",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
)
with path.open("wb") as handle:
    plistlib.dump(info, handle)
PY

codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"
printf 'Built: %s\n' "$app"
