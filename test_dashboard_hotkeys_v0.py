import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HTML = ROOT / "dashboard.html"
FROZEN_SUBMIT_HASH = "1fcc7b3c8d861a62de440c1a7afb15f4b75727bd1f385d4d605ebdcd5e25fc6b"


def function_source(html, name):
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\s*\{{.*?\n\}}", html, re.S)
    assert match, f"missing {name}"
    return match.group()


def block(html, start, end):
    return html.split(start, 1)[1].split(end, 1)[0]


def test_submit_serializer_remains_byte_identical():
    submit = function_source(HTML.read_text(), "submitTrade")
    assert hashlib.sha256(submit.encode()).hexdigest() == FROZEN_SUBMIT_HASH


def test_single_keys_only_invoke_existing_non_destructive_actions():
    html = HTML.read_text()
    handler = block(html, "function handleDashboardHotkey", "document.addEventListener")
    action_map = block(handler, "const actions = {", "};")
    assert "quoteContract()" in action_map
    assert "runPrecheck()" in action_map
    assert "setQuantityPreset" in action_map
    assert "toggleDetail('exitSettings')" in action_map
    assert "toggleDetail('experimentalSettings')" in action_map
    assert "submitTrade" not in action_map
    assert "liquidate" not in action_map
    assert "isTypingTarget(event.target)" in handler
    assert "if (event.repeat) return" in handler


def test_engage_and_flatten_require_deliberate_modifier_chords():
    handler = block(HTML.read_text(), "function handleDashboardHotkey", "document.addEventListener")
    assert "(event.metaKey || event.ctrlKey) && key === 'Enter'" in handler
    assert "event.altKey && event.shiftKey && lower === 'x'" in handler
    assert handler.count("submitTrade()") == 1
    assert handler.count("liquidateAll()") == 1
    assert "if (event.metaKey || event.ctrlKey || event.altKey" in handler


def test_visible_flatten_requires_arm_then_confirm_and_calls_existing_action():
    html = HTML.read_text()
    arm = block(html, "function armFlattenQuickAction", "function isTypingTarget")
    assert "destructiveArm.action === 'flatten'" in arm
    assert "now <= destructiveArm.expiresAt" in arm
    assert "expiresAt:now + 5000" in arm
    assert "return liquidateAll()" in arm
    assert 'onclick="armFlattenQuickAction()"' in html
    assert "confirm(" not in html and "prompt(" not in html


def test_presets_and_legend_use_existing_controls_and_actions():
    html = HTML.read_text()
    preset = function_source(html, "applyContractPreset")
    assert "await loadExpirations()" in preset
    assert "fetch(" not in preset
    assert 'onclick="setQuantityPreset(1)"' in html
    assert 'onclick="setQuantityPreset(2)"' in html
    assert 'onclick="setQuantityPreset(5)"' in html
    assert 'id="hotkeyLegend"' in html
    assert (ROOT / "HOTKEYS_V0.md").exists()


def test_refresh_is_regular_in_place_and_context_remains_isolated():
    html = HTML.read_text()
    scheduler = function_source(html, "scheduleRefresh")
    assert "performance.now() + cadenceMs" in scheduler
    assert "while (nextAt <= now) nextAt += cadenceMs" in scheduler
    assert "scheduleRefresh(refreshContextPanel, 5000)" in html
    assert "scheduleRefresh(updateContextAgeLabel, 1000)" in html
    assert 'id="contextUpdated"' in html
    additions = block(html, "function setQuantityPreset", "// recompute est. cost")
    assert "/api/" not in additions
    assert "study" not in additions.lower()
    assert "admission" not in additions.lower()
    assert "guard" not in additions.lower()
