#!/bin/bash
# Run this once after first deploy to import n8n workflows.
# Usage (from VM): cd /opt/lidl && bash docker/n8n/setup-workflows.sh
set -e

COMPOSE_FILE="$(cd "$(dirname "$0")/.." && pwd)/docker-compose.yml"
WORKFLOWS_DIR="/workflows"

echo "Waiting for n8n to be healthy..."
until curl -sf http://localhost:5678/healthz > /dev/null 2>&1; do
  printf '.'
  sleep 3
done
echo " ready."

echo "Importing workflows..."
docker compose -f "$COMPOSE_FILE" exec -T n8n \
  n8n import:workflow --input="$WORKFLOWS_DIR" --separate

echo ""
echo "Done. Workflows imported."
echo ""
echo "Next steps:"
echo "  1. Open n8n UI: ssh -L 5678:localhost:5678 debian@VM_IP then http://localhost:5678"
echo "  2. Link Signal: make signal-link VM_IP=<ip>"
echo "  3. Scan the QR code in Signal -> Settings -> Linked Devices"
echo "  4. Configure Signal webhook: make signal-webhook VM_IP=<ip> SIGNAL_PHONE=<number>"
echo "  5. Trigger first receipt sync: curl -X POST http://localhost:8000/update"
