#!/usr/bin/env bash
# Acceptance criterion §15.1: this is a NON-embedded app. Fail if anyone adds
# Shopify Polaris or App Bridge.
set -euo pipefail
cd "$(dirname "$0")/.."
pattern='polaris|app-bridge|appbridge|app_bridge|shopify-app-bridge|cdn\.shopify\.com/shopifycloud/app-bridge'
if grep -RniE "$pattern" apps config templates static/js static/src pyproject.toml 2>/dev/null; then
  echo "✗ Found Polaris / App Bridge references (see above). This app must stay non-embedded."
  exit 1
fi
echo "✓ No Polaris / App Bridge references."
