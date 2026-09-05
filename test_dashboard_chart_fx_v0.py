import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent
HTML = ROOT / "dashboard.html"
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
FROZEN_SUBMIT_HASH = "1fcc7b3c8d861a62de440c1a7afb15f4b75727bd1f385d4d605ebdcd5e25fc6b"
FROZEN_SERVER_HASH = "ba20c7cd084825a57a2326dbafe98b89c28fb5a76e23ec8659d8d94e362838e7"


def function_source(html, name):
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\s*\{{.*?\n\}}", html, re.S)
    assert match, f"missing {name}"
    return match.group()


def run_javascript(source, expression):
    if not JSC.exists():
        pytest.skip("JavaScriptCore shell unavailable")
    result = subprocess.run([str(JSC), "-e", source + f"\nprint(JSON.stringify({expression}));"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout.strip())


def test_submit_serializer_and_server_remain_frozen():
    html = HTML.read_text()
    assert hashlib.sha256(function_source(html, "submitTrade").encode()).hexdigest() == FROZEN_SUBMIT_HASH
    assert hashlib.sha256((ROOT / "scalp_server.py").read_bytes()).hexdigest() == FROZEN_SERVER_HASH


def test_wall_glow_intensity_increases_as_spot_nears_wall():
    html = HTML.read_text()
    function = function_source(html, "wallGlowIntensity")
    observed = run_javascript(function, "[wallGlowIntensity(100,100),wallGlowIntensity(99,100),wallGlowIntensity(98,100),wallGlowIntensity(90,100)]")
    assert observed == [1, 0.5, 0, 0]
    render = function_source(html, "renderMarketStructure")
    assert "18 * callGlow" in render and "18 * putGlow" in render
    assert "isClean ? wallGlowIntensity" in render


def test_tick_direction_is_clean_change_only_and_css_is_stable():
    html = HTML.read_text()
    function = function_source(html, "structureTickDirection")
    observed = run_javascript(function, "[structureTickDirection(100,101,true),structureTickDirection(101,100,true),structureTickDirection(100,100,true),structureTickDirection(100,101,false),structureTickDirection(null,101,true)]")
    assert observed == ["up", "down", None, None, None]
    update = function_source(html, "updateStructureSpot")
    assert "structure-tick-up" in update and "structure-tick-down" in update
    assert "void element.offsetWidth" in update
    assert "font-variant-numeric:tabular-nums" in html
    assert "animation:structureTickUp .5s" in html and "animation:structureTickDown .5s" in html


def test_reticle_uses_native_cyan_cross_and_hover_only_brackets():
    html = HTML.read_text()
    render = function_source(html, "renderMarketStructure")
    bind = function_source(html, "bindStructureReticle")
    hide = function_source(html, "hideStructureReticle")
    corners = function_source(html, "structureCornerGraphics")
    assert "axisPointer:{type:'cross'" in render
    assert "color:'#41d3f0'" in render
    assert "type:'dashed'" in render
    assert "mousemove" in bind and "globalout" in bind
    assert "setStructureReticleVisible(inside)" in bind
    assert "currTrigger:'leave'" in hide and "hideTip" in hide
    assert "invisible:!visible" in corners and len(re.findall(r"\[.*?\]", corners)) >= 4


def test_stale_and_not_started_suppress_all_fx_and_keep_isolation():
    html = HTML.read_text()
    state = function_source(html, "renderStructureState")
    render = function_source(html, "renderMarketStructure")
    protected = function_source(html, "submitTrade") + function_source(html, "runPrecheck")
    assert "structureFeedClean = state === 'LIVE'" in state
    assert "hideStructureReticle()" in state
    assert "classList.remove('structure-tick-up', 'structure-tick-down')" in state
    assert "const isClean = payload.status === 'LIVE'" in render
    assert "show:isClean" in render and "triggerOn:isClean" in render
    assert "graphic:structureCornerGraphics(structureChart, false)" in render
    assert "structure" not in protected.lower()
    assert "/api/order" not in render
    assert "requestAnimationFrame" not in html

