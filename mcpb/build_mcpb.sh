#!/usr/bin/env bash
# Build the Claude Desktop Extension bundle (wow-aisuite.mcpb).
#
# A .mcpb is a plain zip: manifest.json + icon at the root, and the stdio server
# it launches under server/. We reuse the published npm launcher (npm/) verbatim
# — bin/cli.js bridges stdio to the remote OAuth-protected endpoint — and bundle
# its mcp-remote dependency so the extension installs and runs offline, without
# an npx fetch. The endpoint stays baked into cli.js on purpose: we move it by
# shipping a new version, not by editing thousands of installed configs.
#
# Output: mcpb/dist/wow-aisuite.mcpb
#
# Run:  bash mcpb/build_mcpb.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAGE="$(mktemp -d)"
OUT="$HERE/dist"
trap 'rm -rf "$STAGE"' EXIT

echo "==> stage"
# Root of the bundle: the manifest and icon Claude Desktop reads directly.
cp "$HERE/manifest.json" "$STAGE/manifest.json"
cp "$HERE/icon.png"      "$STAGE/icon.png"

# The launcher and its bundled dependency, exactly as published to npm.
mkdir -p "$STAGE/server"
cp -R "$ROOT/npm/bin"          "$STAGE/server/bin"
cp    "$ROOT/npm/package.json" "$STAGE/server/package.json"
cp    "$ROOT/npm/LICENSE"      "$STAGE/server/LICENSE"
# mcp-remote (+ its deps) so the extension needs no network to start itself.
cp -R "$ROOT/npm/node_modules" "$STAGE/server/node_modules"

echo "==> validate manifest"
# The official CLI validates the manifest against the current schema and packs a
# canonical zip. Prefer it; fall back to a plain zip when it or the network is
# unavailable (Claude Desktop only needs a valid zip, however it was produced).
mkdir -p "$OUT"
if npx --yes @anthropic-ai/mcpb pack "$STAGE" "$OUT/wow-aisuite.mcpb" >/dev/null 2>&1; then
  echo "   packed with @anthropic-ai/mcpb"
elif npx --yes @modelcontextprotocol/mcpb pack "$STAGE" "$OUT/wow-aisuite.mcpb" >/dev/null 2>&1; then
  echo "   packed with @modelcontextprotocol/mcpb"
else
  echo "   mcpb CLI unavailable — packing a plain zip"
  ( cd "$STAGE" && zip -qr -X "$OUT/wow-aisuite.mcpb" manifest.json icon.png server )
fi

echo "==> done"
ls -la "$OUT/wow-aisuite.mcpb"
echo "size: $(du -h "$OUT/wow-aisuite.mcpb" | cut -f1)"
