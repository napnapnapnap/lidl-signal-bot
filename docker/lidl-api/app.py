import json
import os
import subprocess
import threading
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

app = FastAPI()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))
ITEM_GROUPS_PATH = DATA_DIR / "item_groups.json"

STOPWORDS = {
    "le", "la", "les", "de", "du", "des", "et", "au", "aux",
    "bio", "vrac", "fairtrade", "xl", "kg", "g", "x",
}

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _base_cmd() -> list[str]:
    return [
        "python3", str(SCRIPTS_DIR / "lidl_receipts.py"),
        "--data-dir", str(DATA_DIR),
        "--country", os.environ.get("LIDL_COUNTRY", "FR"),
        "--login",
    ]


def _start_job(cmd: list[str]) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "output": None}

    def run() -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            output = result.stdout + result.stderr
            status = "done" if result.returncode == 0 else "error"
        except Exception as exc:
            output = str(exc)
            status = "error"
        with _jobs_lock:
            _jobs[job_id]["status"] = status
            _jobs[job_id]["output"] = output

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status() -> dict:
    summaries_path = DATA_DIR / "receipts_summaries.json"
    detail_path = DATA_DIR / "receipts_detail.json"
    auth_state_path = DATA_DIR / "lidl_auth_state.json"

    last_sync = None
    receipt_count = 0

    if summaries_path.exists():
        data = json.loads(summaries_path.read_text())
        last_sync = data.get("fetched_at")

    if detail_path.exists():
        data = json.loads(detail_path.read_text())
        receipt_count = data.get("total_receipts", 0)

    return {
        "auth_valid": auth_state_path.exists(),
        "last_sync": last_sync,
        "receipt_count": receipt_count,
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/update")
def update() -> dict:
    summaries_path = DATA_DIR / "receipts_summaries.json"
    subcmd = "all" if not summaries_path.exists() else "update"
    job_id = _start_job(_base_cmd() + [subcmd])
    return {"job_id": job_id}


@app.post("/reauth")
def reauth() -> dict:
    job_id = _start_job(_base_cmd() + ["auth-check"])
    return {"job_id": job_id}


@app.get("/query")
def query(
    days: Optional[float] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_articles: bool = False,
) -> dict:
    cmd = _base_cmd() + ["query"]
    if days is not None:
        days_str = str(int(days)) if days == int(days) else str(days)
        cmd += ["--days", days_str]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if include_articles:
        cmd += ["--include-articles"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        err = result.stderr or result.stdout
        if "unauthorized" in err.lower() or "expired" in err.lower():
            raise HTTPException(status_code=401, detail=err)
        raise HTTPException(status_code=500, detail=err)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"Invalid script output: {result.stdout[:500]}")


def _normalize(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _first_significant_word(name: str) -> str:
    words = _normalize(name).split()
    for word in words:
        clean = word.strip(".,!?-()")
        if clean and clean not in STOPWORDS and len(clean) > 1:
            return clean
    return words[0] if words else name


def _load_groups() -> dict[str, str]:
    groups_path = DATA_DIR / "item_groups.json"
    if groups_path.exists():
        return json.loads(groups_path.read_text())
    return {}


def _save_groups(groups: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    groups_path = DATA_DIR / "item_groups.json"
    groups_path.write_text(
        json.dumps(groups, indent=2, ensure_ascii=False) + "\n"
    )


def _assign_group(name: str, groups: dict[str, str]) -> str:
    norm = _normalize(name)
    tokens = set(norm.split())
    candidates = [
        k for k in groups
        if (" " in k and k in norm)                        # multi-word key: substring OK
        or all(t in tokens for t in k.split())             # single-word key: whole token match
    ]
    best_key = max(candidates, key=len, default=None)
    if best_key:
        return groups[best_key]
    key = _first_significant_word(name)
    display = next(
        (w for w in name.split() if _normalize(w).strip(".,!?-()") == key),
        name.split()[0] if name.strip() else key,
    )
    groups[key] = display
    return display


@app.get("/top")
def top(days: int = Query(default=30, ge=1)) -> dict:
    detail_path = DATA_DIR / "receipts_detail.json"
    if not detail_path.exists():
        raise HTTPException(status_code=404, detail="No receipts data. Run /update first.")

    data = json.loads(detail_path.read_text())
    groups = _load_groups()
    counts: dict[str, int] = defaultdict(int)
    new_keys = False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for receipt in data.get("receipts", []):
        date_str = receipt.get("date")
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except ValueError:
                pass

        for article in receipt.get("articles", []):
            desc = (article.get("description") or "").strip()
            if not desc:
                continue
            before = len(groups)
            group = _assign_group(desc, groups)
            if len(groups) > before:
                new_keys = True
            counts[group] += 1

    if new_keys:
        _save_groups(groups)

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return {
        "days": days,
        "items": [{"name": name, "count": count} for name, count in ranked],
    }
