#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

rm -rf "${PROJECT_ROOT}/engine/build"
rm -rf "${PROJECT_ROOT}/web/dist"

echo "Removed local build directories. Tracked web/public/engine artifacts were kept."
