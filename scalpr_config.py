"""Validated, hash-stamped configuration authority for new Scalpr v1 systems.

The file is JSON syntax inside ``config/scalpr.yaml`` so it remains valid YAML
without adding a runtime YAML dependency.  Frozen legacy constants are not read,
moved, or overridden here.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import feature_engine as fe


CONFIG_PATH = Path(__file__).resolve().parent / "config/scalpr.yaml"


def load_entry_intelligence_config(path=CONFIG_PATH) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != "scalpr-config-v1":
        raise ValueError("unsupported Scalpr config schema")
    config = deepcopy(document.get("entry_intelligence_v1") or {})
    required = {
        "config_version", "mode", "feature_flag", "enabled_default",
        "formal_cohort_eligible", "collection_role", "symbol", "universe",
        "direction", "execution", "contract_assembly", "episode", "outcome", "collector",
        "operator_approval",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"entry intelligence config missing: {','.join(missing)}")
    if config["mode"] != "PAPER_SHADOW_ONLY":
        raise ValueError("entry intelligence must remain paper/shadow only")
    if config["enabled_default"] is not False:
        raise ValueError("entry intelligence collector must default off")
    if config["formal_cohort_eligible"] is not False:
        raise ValueError("pre-lock capture cannot be cohort eligible")
    if config["collection_role"] != "PRELOCK_DRY_RUN":
        raise ValueError("collector role must remain PRELOCK_DRY_RUN")
    if config["symbol"] != "SPY":
        raise ValueError("entry intelligence v1 scope is SPY only")
    execution = config["execution"]
    if (int(execution["dte_min"]), int(execution["dte_max"])) != (0, 2):
        raise ValueError("entry intelligence v1 scope is 0-2 DTE")
    assembly = config["contract_assembly"]
    if assembly.get("contract_assembly_version") != "entry-contract-assembly-v2":
        raise ValueError("unsupported contract assembly version")
    if assembly.get("join_key") != "OCC_OPTION_SYMBOL":
        raise ValueError("contract assembly must join by OCC option symbol")
    if float(assembly.get("max_quote_age_seconds")) != float(
            execution["max_quote_age_seconds"]):
        raise ValueError("contract assembly quote freshness drift")
    if assembly.get("snapshot_volume_policy") != (
            "ignore_always_zero_or_missing_is_not_observed_volume"):
        raise ValueError("snapshot volume must remain non-authoritative")
    if not config["operator_approval"].get("approved"):
        raise ValueError("collector thresholds are not operator approved")
    config["config_hash"] = fe.canonical_hash(config)
    return config


def entry_bid_capture_enabled(environ=None) -> bool:
    """Strict opt-in: unset, blank, or any value other than ``1`` is off."""
    env = os.environ if environ is None else environ
    config = load_entry_intelligence_config()
    return env.get(config["feature_flag"], "") == "1"
