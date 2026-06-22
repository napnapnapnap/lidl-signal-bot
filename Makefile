.PHONY: smoke card-totals

PYTHON ?= python3

smoke:
	$(PYTHON) -m py_compile scripts/lidl_receipts.py
	$(PYTHON) scripts/lidl_receipts.py --help >/dev/null
	@if [ -f data/receipts_summaries.json ]; then $(PYTHON) scripts/lidl_receipts.py status >/dev/null; fi
	@if [ -f data/receipts_detail.json ]; then $(PYTHON) scripts/lidl_receipts.py query --days 1 >/dev/null; fi

card-totals:
	$(PYTHON) scripts/lidl_receipts.py card-totals

VM_USER ?= debian
VM_IP   ?=

.PHONY: ssh-tunnel ssh-vm reauth signal-link signal-webhook setup-workflows

ssh-tunnel:
	ssh -i ~/.ssh/lidl_bot -L 5678:localhost:5678 $(VM_USER)@$(VM_IP)

ssh-vm:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP)

reauth:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP) \
		"cd /opt/lidl/docker && docker compose exec lidl-api \
		python3 /scripts/lidl_receipts.py auth-check --login \
		--data-dir /data --country FR"

signal-link:
	@echo "Generating Signal link QR code (scan via Signal → Settings → Linked Devices → Link New Device)..."
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP) \
		"sudo apt-get install -y -qq qrencode 2>/dev/null; \
		cd /opt/lidl/docker && docker compose exec signal-cli-rest-api \
		signal-cli --config /home/.local/share/signal-cli link -n LidlBot 2>&1 | grep -m1 'sgnl://' | qrencode -t UTF8"

signal-webhook:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP) \
		"curl -sf -X POST http://localhost:8080/v1/configuration/account/$(SIGNAL_PHONE)/settings \
		-H 'Content-Type: application/json' \
		-d '{\"webhook\":{\"url\":\"http://n8n:5678/webhook/signal\"}}'"

setup-workflows:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP) \
		"cd /opt/lidl && bash docker/n8n/setup-workflows.sh"
