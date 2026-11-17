#!/usr/bin/env bash
# Downloads pinned front-end files into static/vendor and the Tailwind CLI into bin/.
# No Node.js needed. Re-run after changing a version below:  make vendor
set -euo pipefail
cd "$(dirname "$0")/.."

HTMX_VERSION=2.0.4
HTMX_SSE_VERSION=2.2.2
ALPINE_VERSION=3.14.8
CHARTJS_VERSION=4.4.7
LUCIDE_VERSION=0.469.0
TAILWIND_VERSION=4.1.11

V=static/vendor
mkdir -p "$V" bin

fetch() { curl -fsSL "$1" -o "$2"; echo "  ✓ $2"; }

echo "Downloading JS libraries…"
fetch "https://cdn.jsdelivr.net/npm/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" "$V/htmx.min.js"
fetch "https://cdn.jsdelivr.net/npm/htmx-ext-sse@${HTMX_SSE_VERSION}/sse.js" "$V/htmx-sse.js"
fetch "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js" "$V/alpine.min.js"
fetch "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.js" "$V/chart.umd.js"

# Remove "sourceMappingURL" comments: we don't ship .map files, and Django's
# production static storage fails the build on references to missing files.
for f in "$V"/*.js; do sed -i.bak '/sourceMappingURL=/d' "$f" && rm -f "$f.bak"; done

echo "Downloading Lucide icons…"
tmp=$(mktemp -d)
curl -fsSL "https://registry.npmjs.org/lucide-static/-/lucide-static-${LUCIDE_VERSION}.tgz" -o "$tmp/lucide.tgz"
tar -xzf "$tmp/lucide.tgz" -C "$tmp"
rm -rf "$V/lucide" && mkdir -p "$V/lucide"
cp "$tmp"/package/icons/*.svg "$V/lucide/"
rm -rf "$tmp"
echo "  ✓ $(ls "$V/lucide" | wc -l | tr -d ' ') icons in $V/lucide"

echo "Downloading Tailwind CSS standalone CLI…"
os=$(uname -s | tr '[:upper:]' '[:lower:]'); arch=$(uname -m)
case "$os" in darwin) os=macos ;; esac
case "$arch" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; esac
# Apple Silicon shells running under Rosetta report x86_64; use the native build.
if [ "$os" = macos ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then arch=arm64; fi
fetch "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${os}-${arch}" bin/tailwindcss
chmod +x bin/tailwindcss
echo "Done."
