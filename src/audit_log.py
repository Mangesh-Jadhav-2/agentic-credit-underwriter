"""
Persistent, tamper-evident audit log (JSON Lines).

One record per underwriting run, appended to  <AUDIT_LOG_DIR>/audit_log.jsonl  (default ./logs).
Every record carries a SHA-256 hash over its own content AND the previous record's hash, so editing,
deleting or reordering any past record breaks the chain and `verify_chain` reports where.

What a record proves: which inputs (hash), under which policy version and model, produced which
outcome, with every override and caveat the run saw. It stores no secrets (no API keys, no email).
"""
import datetime as dt
import hashlib
import json
import os
from typing import Any, Dict, List, Tuple

GENESIS = "0" * 64
LOG_NAME = "audit_log.jsonl"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def log_path() -> str:
    return os.path.join(os.getenv("AUDIT_LOG_DIR", "logs"), LOG_NAME)


def _last_hash(path: str) -> str:
    if not os.path.exists(path):
        return GENESIS
    last = None
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last)["record_hash"] if last else GENESIS


def build_record(run_id: str, company_data: Dict[str, Any], state: Dict[str, Any], policy: Dict[str, Any],
                 model: str) -> Dict[str, Any]:
    metrics = state.get("computed_metrics")
    decision = state.get("final_decision")
    inputs = {k: company_data.get(k) for k in
              ("company_name", "ticker", "currency", "fiscal_year_end", "financials", "provenance", "source")}
    return {
        "run_id": run_id,
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "company": {"name": state.get("company_name"), "ticker": state.get("ticker")},
        "policy_version": policy.get("version"),
        "policy_values": policy.get("values"),
        "decision_rules": policy.get("decision_rules"),
        "unconfirmed_policy_values": policy.get("unconfirmed"),
        "llm_model": model,
        "input_hash": sha256(inputs),
        "inputs": inputs,                      # the figures themselves, so a reviewer can check them without re-fetching
        "source": company_data.get("source"),
        "analyst_overrides": company_data.get("analyst_overrides", []),
        "override_reason": company_data.get("override_reason", ""),
        "data_quality_status": (state.get("data_quality") or {}).get("status"),
        "data_quality_issues": [{"severity": i["severity"], "code": i["code"], "field": i.get("field")}
                                for i in (state.get("data_quality") or {}).get("issues", [])],
        "qualitative_status": state.get("qualitative_status"),
        "qualitative_evidence_hash": sha256(state.get("qualitative_evidence") or []),
        "qualitative_evidence_ids": [e.get("id") for e in (state.get("qualitative_evidence") or [])],
        "extracted_risks": state.get("extracted_risks", []),
        "metrics": metrics.model_dump() if hasattr(metrics, "model_dump") else metrics,
        "policy_violations": state.get("policy_violations", []),
        "decision_status": state.get("decision_status"),
        "decision": decision,
        "output_hash": sha256(decision),
        "audit_trail": state.get("audit_trail", []),
    }


def append_record(record: Dict[str, Any], path: str = None) -> Dict[str, str]:
    """Append with hash chaining. Returns {'path', 'record_hash', 'prev_hash'}."""
    path = path or log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prev = _last_hash(path)
    body = dict(record, prev_hash=prev)
    body["record_hash"] = sha256(body)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_canonical(body) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return {"path": os.path.abspath(path), "record_hash": body["record_hash"], "prev_hash": prev}


def verify_chain(path: str = None) -> Tuple[bool, List[str]]:
    """(ok, problems). Recomputes every hash and checks each record points at the one before."""
    path = path or log_path()
    problems: List[str] = []
    if not os.path.exists(path):
        return True, []
    prev = GENESIS
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {n}: not valid JSON")
                continue
            claimed = rec.pop("record_hash", None)
            if rec.get("prev_hash") != prev:
                problems.append(f"line {n}: previous-hash link broken (a record was removed, inserted or reordered)")
            if sha256(rec) != claimed:
                problems.append(f"line {n}: content does not match its hash (record was edited)")
            prev = claimed or prev
    return (not problems), problems
