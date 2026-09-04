#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This packaging script requires macOS." >&2
  exit 1
fi
"$repo_root/scripts/build_macos_app.sh"

app="$repo_root/dist/AgentLoop.app"
[[ -d "$app" ]] || {
  echo "Missing app bundle: $app" >&2
  exit 1
}

version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo_root/pyproject.toml" | head -1)"
output="$repo_root/dist/installers"
pkg="$output/AgentLoop-$version.pkg"
dmg="$output/AgentLoop-$version.dmg"
export TMPDIR="$repo_root/build/tmp"
mkdir -p "$TMPDIR"
stage="$(mktemp -d "$TMPDIR/agentloop-dmg.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

mkdir -p "$output"
rm -f "$pkg" "$dmg"

pkgbuild \
  --component "$app" \
  --install-location /Applications \
  --identifier io.github.still0123.agentloop \
  --version "$version" \
  "$pkg"

ditto "$app" "$stage/AgentLoop.app"
ln -s /Applications "$stage/Applications"
hdiutil create \
  -volname "AgentLoop $version" \
  -srcfolder "$stage" \
  -ov \
  -format UDZO \
  "$dmg"

shasum -a 256 "$pkg" "$dmg"
