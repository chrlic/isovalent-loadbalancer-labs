#!/bin/bash
# Start the Isovalent LB GUI
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8080}"
KUBECTL="${KUBECTL:-kubectl}"

# Check kubectl is available
if ! command -v "$KUBECTL" &>/dev/null; then
  echo "ERROR: kubectl not found. Set KUBECTL=/path/to/kubectl or ensure it is in PATH."
  exit 1
fi

echo "=========================================="
echo "  Isovalent LB GUI"
echo "=========================================="
echo "  URL:     http://localhost:${PORT}"
echo "  kubectl: $(which $KUBECTL)"
echo "=========================================="

exec python3 "${SCRIPT_DIR}/server.py"
