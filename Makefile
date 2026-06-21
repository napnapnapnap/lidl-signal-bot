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

.PHONY: ssh-tunnel ssh-vm

ssh-tunnel:
	ssh -i ~/.ssh/lidl_bot -L 5678:localhost:5678 $(VM_USER)@$(VM_IP)

ssh-vm:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP)
