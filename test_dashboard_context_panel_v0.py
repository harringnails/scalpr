import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard.html"
FROZEN_SUBMIT_HASH = "1fcc7b3c8d861a62de440c1a7afb15f4b75727bd1f385d4d605ebdcd5e25fc6b"
REPRESENTATIVE_PAYLOAD = (
    '{"symbol":"SPY260904C00774000","type":"option","buy":true,'
    '"ladder":[{"at":0,"tol":5},{"at":10,"tol":1},{"at":20,"tol":0.05}],'
    '"stall_seconds":45,"stall_min_profit":20,"grace_seconds":60,'
    '"confirm_ticks":2,"runner_policy":{"enabled":false,'
    '"version":"regime-flow-runner-v1"},"auto_exit_authorized":true,'
    '"climb_adds":{"enabled":false,"add_contracts":1,"max_adds":2},'
    '"request_id":"fixture-request","qty":1}'
)


def function_source(html, name):
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\s*\{{.*?\n\}}", html, re.S)
    assert match, f"missing {name}"
    return match.group()


def test_submit_serializer_source_and_representative_payload_are_frozen():
    html = DASHBOARD.read_text()
    submit = function_source(html, "submitTrade")
    assert hashlib.sha256(submit.encode()).hexdigest() == FROZEN_SUBMIT_HASH

    payload = {}
    payload["symbol"] = "SPY260904C00774000"
    payload["type"] = "option"
    payload["buy"] = True
    payload["ladder"] = [{"at": 0, "tol": 5}, {"at": 10, "tol": 1}, {"at": 20, "tol": 0.05}]
    payload["stall_seconds"] = 45
    payload["stall_min_profit"] = 20
    payload["grace_seconds"] = 60
    payload["confirm_ticks"] = 2
    payload["runner_policy"] = {"enabled": False, "version": "regime-flow-runner-v1"}
    payload["auto_exit_authorized"] = True
    payload["climb_adds"] = {"enabled": False, "add_contracts": 1, "max_adds": 2}
    payload["request_id"] = "fixture-request"
    payload["qty"] = 1
    assert json.dumps(payload, separators=(",", ":")) == REPRESENTATIVE_PAYLOAD


def test_context_panel_is_control_free_and_defaults_to_insufficient_data():
    html = DASHBOARD.read_text()
    panel = re.search(r'<section id="scalprContext".*?</section>', html, re.S).group()
    assert not re.search(r"<(button|input|select|textarea)\b", panel)
    assert "SHADOW · READ-ONLY" in panel
    assert "— INSUFFICIENT DATA" in panel
    assert "EXPLORATORY / NON-INFERENTIAL" in panel
    assert "execution_authority" not in panel


def test_stale_and_missing_context_are_blanked_instead_of_rendered_live():
    html = DASHBOARD.read_text()
    unavailable = function_source(html, "renderContextUnavailable")
    refresh = function_source(html, "refreshContextPanel")
    render = function_source(html, "renderContextRecord")
    assert "STALE — DO NOT INTERPRET" in unavailable
    assert "— INSUFFICIENT DATA" in unavailable
    assert "payload.ledger_status !== 'AVAILABLE'" in refresh
    assert "ageSeconds > CONTEXT_MAX_AGE_SECONDS" in render
    assert "ledgerFreshness === 'STALE'" in render


def test_context_is_absent_from_trade_and_precheck_paths():
    html = DASHBOARD.read_text()
    protected = function_source(html, "submitTrade") + function_source(html, "runPrecheck")
    assert "context" not in protected.lower()
    assert "8421" not in protected
    assert "CONTEXT_SOURCE_URL" not in Path(ROOT / "scalp_server.py").read_text()
