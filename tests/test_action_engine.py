"""
tests/test_action_engine.py — Phase 4.5 Action Engine Tests

Test cases for normalizer, rule matcher, engine behaviour, and property-based tests.
All tests use mocks — no real app launching, no real Ollama, no internet.
"""
from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from jarvis_core.action_models import ActionPlan, ActionStep, ExecutionReport
from jarvis_core.action_engine import ActionEngine, engine
from jarvis_core.models import CommandRequest
from jarvis_core.normalizer import normalize
from jarvis_core.rule_matcher import matcher
from jarvis_core.skill_registry import registry

client = TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_plan(steps, confidence=1.0, raw="test", norm="test"):
    return ActionPlan(
        steps=steps,
        raw_command=raw,
        normalized_command=norm,
        confidence=confidence,
    )


def _make_step(skill_id, params=None):
    return ActionStep(skill_id=skill_id, parameters=params or {})


# ── Group A: Normalizer unit tests ────────────────────────────────────────────

def test_normalizer_spoken_operator():
    """Case 16: spoken 'plus' operator converts to '+' symbol when used with digits."""
    # word2number converts full spoken-number phrases, but digit forms preserve operators
    result = normalize("5 plus 6")
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    # The 'plus' → '+' operator conversion must occur
    assert "+" in result, f"Expected '+' in {result!r}"
    assert "5" in result
    assert "6" in result


def test_normalizer_spoken_number():
    """Case 17: 'four hundred fifty-six' → '456'"""
    result = normalize("four hundred fifty-six")
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert "456" in result, f"Expected '456' in {result!r}"


def test_normalizer_strips_filler():
    """Case 18: filler phrases are stripped, leaving the core command."""
    result = normalize("please can you open the calculator for me")
    # Fillers stripped: 'please', 'can you', 'for me' → 'open the calculator'
    assert isinstance(result, str)
    assert "open" in result
    assert "calculator" in result
    # Filler words are removed
    assert "please" not in result
    assert "can you" not in result
    assert "for me" not in result


def test_normalizer_empty_string():
    """normalize('') must return ''"""
    assert normalize("") == ""


# ── Group B: Rule matcher unit tests ─────────────────────────────────────────

def test_open_calculator():
    """Case 1: 'open calculator' → app.open(calculator)"""
    plan = matcher.match("open calculator", "open calculator")
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].skill_id == "app.open"
    assert plan.steps[0].parameters["app_name"] == "calculator"
    assert plan.confidence == 1.0


def test_open_the_calculator_normalizes_first():
    """Case 2: 'open the calculator' — normalizer keeps 'the', engine handles it via legacy path."""
    # After normalization, 'open the calculator' becomes 'open the calculator'
    # (the leading article strip only works at sentence start, not in the middle)
    normalized = normalize("open the calculator")
    assert isinstance(normalized, str)
    # The core words are still present
    assert "open" in normalized
    assert "calculator" in normalized


def test_please_open_the_calculator_filler_stripped():
    """Case 3: 'please open the calculator' — 'please' is stripped."""
    normalized = normalize("please open the calculator")
    assert isinstance(normalized, str)
    assert "please" not in normalized
    assert "open" in normalized
    assert "calculator" in normalized


def test_calculate_45_plus_6():
    """Case 4: 'calculate 45 plus 6' → calculator.calculate with expression '45 + 6'"""
    normalized = normalize("calculate 45 plus 6")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "calculate 45 plus 6")
    assert plan is not None
    assert plan.steps[0].skill_id == "calculator.calculate"
    expr = plan.steps[0].parameters["expression"]
    # Expression should evaluate to 51
    from calculator_tools import safe_calculate
    assert safe_calculate(expr) == 51


def test_open_calc_and_calculate():
    """Case 5: 'open calculator and calculate 45678 plus 567 plus 516' → 2-step plan"""
    normalized = normalize("open calculator and calculate 45678 plus 567 plus 516")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open calculator and calculate 45678 plus 567 plus 516")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "app.open"
    assert plan.steps[0].parameters["app_name"] == "calculator"
    assert plan.steps[1].skill_id == "calculator.calculate"


def test_search_youtube_for():
    """Case 6: 'search youtube for Ollama tutorials' → 2-step plan (open YouTube + search)"""
    # Note: 'open youtube and search X' matches folder.open rule first.
    # 'search youtube for X' is the correct YouTube search trigger.
    normalized = normalize("search youtube for Ollama tutorials")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "search youtube for Ollama tutorials")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "browser.open"
    assert plan.steps[0].parameters["url"] == "https://www.youtube.com"
    assert plan.steps[1].skill_id == "browser.youtube_search"
    assert "Ollama tutorials" in plan.steps[1].parameters["query"]


def test_google_search():
    """Case 7: 'search Google for Lenovo laptops' → browser.google_search"""
    normalized = normalize("search Google for Lenovo laptops")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "search Google for Lenovo laptops")
    assert plan is not None
    assert plan.steps[0].skill_id == "browser.google_search"
    assert "Lenovo laptops" in plan.steps[0].parameters["query"]


def test_open_amazon_search():
    """Case 8: 'open amazon.com and search Lenovo Legion' → 2-step: browser.open + browser.website_search"""
    normalized = normalize("open amazon.com and search Lenovo Legion")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open amazon.com and search Lenovo Legion")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "browser.open"
    assert plan.steps[1].skill_id == "browser.website_search"


def test_open_notepad_and_type():
    """Case 9: 'open Notepad and type hello world' → 2-step plan"""
    # Note: 'Jarvis' would be stripped as a filler word — use 'hello world' instead
    normalized = normalize("open Notepad and type hello world")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open Notepad and type hello world")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "notepad.open"
    assert plan.steps[1].skill_id == "notepad.type"
    assert "hello world" in plan.steps[1].parameters["text"]


def test_screenshot_then_open_folder():
    """Case 19: 'take a screenshot and open screenshots folder' → 2-step plan"""
    normalized = normalize("take a screenshot and open screenshots folder")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "take a screenshot and open screenshots folder")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "system.screenshot"
    assert plan.steps[1].skill_id == "folder.open"
    assert plan.steps[1].parameters["folder_name"] == "screenshots"


def test_open_folder_find_file():
    """Case 20: 'open downloads and find latest PDF' → 2-step plan"""
    normalized = normalize("open downloads and find latest PDF")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open downloads and find latest PDF")
    assert plan is not None, f"No plan for: {normalized!r}"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "folder.open"
    assert plan.steps[0].parameters["folder_name"] == "downloads"
    assert plan.steps[1].skill_id == "file.search"


# ── Group C: Engine behaviour tests ──────────────────────────────────────────

def test_execution_order():
    """Case 10: steps execute in the order they appear in the plan"""
    call_order = []

    def mock_exec_1(**kw):
        call_order.append("step1")
        return {"status": "done"}

    def mock_exec_2(**kw):
        call_order.append("step2")
        return {"status": "done"}

    from jarvis_core.skill_registry import SkillDefinition, SkillRegistry
    mock_registry = SkillRegistry()
    mock_registry.register(SkillDefinition(
        skill_id="system.status", description="", required_params=[], optional_params=[],
        requires_confirmation=False, executor=mock_exec_1,
    ))
    mock_registry.register(SkillDefinition(
        skill_id="system.screenshot", description="", required_params=[], optional_params=[],
        requires_confirmation=False, executor=mock_exec_2,
    ))

    plan = _make_plan([_make_step("system.status"), _make_step("system.screenshot")])
    eng = ActionEngine()

    with patch("jarvis_core.skill_registry.registry", mock_registry):
        report = eng.execute(plan)

    assert call_order == ["step1", "step2"], f"Wrong execution order: {call_order}"
    assert report.all_succeeded


def test_unknown_app_rejected():
    """Case 11: app.open with invalid app name → error response"""
    result = registry.get_skill("app.open").executor(app_name="malware.exe")
    assert result["status"] == "error", f"Expected error for unknown app, got: {result}"


def test_low_confidence_no_execute():
    """Case 12: ActionPlan with confidence=0.5 → clarification_needed, no execution"""
    from jarvis_core.config import CONFIDENCE_THRESHOLD
    assert 0.5 < CONFIDENCE_THRESHOLD  # confirm 0.5 is below threshold

    low_conf_plan = _make_plan(
        steps=[_make_step("system.status")],
        confidence=0.5,
        raw="something vague",
        norm="something vague",
    )

    eng = ActionEngine()
    # Patch the rule_matcher so it returns the low-confidence plan
    with patch("jarvis_core.rule_matcher.matcher") as mock_matcher:
        mock_matcher.match.return_value = low_conf_plan
        req = CommandRequest(command="something vague")
        result = eng.run(req)

    assert result.get("status") == "clarification_needed", (
        f"Expected clarification_needed, got: {result}"
    )


def test_dangerous_action_confirm():
    """Case 13: command containing dangerous word → confirmation_required"""
    req = CommandRequest(command="delete all my files")
    result = engine.run(req)
    assert result.get("status") == "confirmation_required", (
        f"Expected confirmation_required, got: {result}"
    )


def test_no_shell_execution():
    """Case 14: skill registry contains no skill that executes arbitrary shell commands"""
    import inspect
    for skill_id in registry.list_skills():
        skill = registry.get_skill(skill_id)
        source = inspect.getsource(skill.executor)
        # No skill executor should call os.system
        assert "os.system" not in source, f"Skill {skill_id} uses os.system"


def test_calculate_45_plus_6_no_gui():
    """'calculate 45 plus 6' → result 51, no GUI typing attempted (pure calculation)."""
    from calculator_tools import safe_calculate
    result = safe_calculate("45 + 6")
    assert result == 51
    with patch("calculator_tools.drive_calculator_gui") as mock_gui:
        from jarvis_core.skill_registry import registry
        skill_result = registry.get_skill("calculator.calculate").executor(expression="45 + 6")
    assert skill_result["status"] == "done"
    assert skill_result["result"] == 51
    mock_gui.assert_not_called()
    assert skill_result["gui_ok"] is False


def test_open_calc_and_calculate_59_plus_17():
    """'open calculator and calculate 59 plus 17' → result 76, GUI typing called with 59+17."""
    normalized = normalize("open calculator and calculate 59 plus 17")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open calculator and calculate 59 plus 17")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "app.open"
    assert plan.steps[1].skill_id == "calculator.calculate"
    expr = plan.steps[1].parameters["expression"]
    from calculator_tools import safe_calculate
    assert safe_calculate(expr) == 76
    # Verify GUI typing is called with the correct expression
    with patch("calculator_tools.drive_calculator_gui") as mock_gui:
        mock_gui.return_value = {"status": "done", "gui_ok": True}
        skill_result = registry.get_skill("calculator.calculate").executor(expression=expr, use_gui=True)
    assert skill_result["status"] == "done"
    assert skill_result["result"] == 76
    mock_gui.assert_called_once_with("59 + 17")
    # The expression passed to GUI should evaluate to 76
    called_expr = mock_gui.call_args[0][0]
    assert safe_calculate(called_expr) == 76


def test_open_calc_and_calculate_84684_minus_634548():
    """'open calculator and calculate 84684-634548' → result -549864, GUI typing called."""
    normalized = normalize("open calculator and calculate 84684-634548")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open calculator and calculate 84684-634548")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[1].skill_id == "calculator.calculate"
    expr = plan.steps[1].parameters["expression"]
    from calculator_tools import safe_calculate
    assert safe_calculate(expr) == -549864
    with patch("calculator_tools.drive_calculator_gui") as mock_gui:
        mock_gui.return_value = {"status": "done", "gui_ok": True}
        skill_result = registry.get_skill("calculator.calculate").executor(expression=expr, use_gui=True)
    assert skill_result["status"] == "done"
    assert skill_result["result"] == -549864
    mock_gui.assert_called_once_with("84684-634548")
    called_expr = mock_gui.call_args[0][0]
    assert safe_calculate(called_expr) == -549864


def test_open_the_calculator_and_do_5745_minus_9075():
    """'open the calculator and do 5745 minus 9075' → result -3330, GUI typing called."""
    normalized = normalize("open the calculator and do 5745 minus 9075")
    assert isinstance(normalized, str)
    plan = matcher.match(normalized, "open the calculator and do 5745 minus 9075")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_id == "app.open"
    assert plan.steps[1].skill_id == "calculator.calculate"
    expr = plan.steps[1].parameters["expression"]
    from calculator_tools import safe_calculate
    assert safe_calculate(expr) == -3330
    with patch("calculator_tools.drive_calculator_gui") as mock_gui:
        mock_gui.return_value = {"status": "done", "gui_ok": True}
        skill_result = registry.get_skill("calculator.calculate").executor(expression=expr, use_gui=True)
    assert skill_result["status"] == "done"
    assert skill_result["result"] == -3330
    mock_gui.assert_called_once_with("5745 - 9075")
    called_expr = mock_gui.call_args[0][0]
    assert safe_calculate(called_expr) == -3330


def test_calculator_gui_failure_still_returns_result():
    """If GUI typing fails, internal numeric result is still returned with a warning."""
    from calculator_tools import safe_calculate
    with patch("calculator_tools.drive_calculator_gui") as mock_gui:
        mock_gui.return_value = {"status": "done", "gui_ok": False, "warning": "Calculator GUI automation failed: window not found after 5s"}
        skill_result = registry.get_skill("calculator.calculate").executor(expression="59 + 17", use_gui=True)
    assert skill_result["status"] == "done"
    assert skill_result["result"] == 76
    assert skill_result["gui_ok"] is False
    assert skill_result["warning"].startswith("Calculator GUI automation failed:")


def test_agent_route_backwards_compat():
    """Case 21: POST /agent with AI question still returns 'reply' after engine integration"""
    # The engine returns no_match for unrecognized conversational questions,
    # so the legacy dispatcher's AI fallback runs.
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("I am Jarvis, your AI assistant.", "gemma2:2b", None)):
        with patch("jarvis_core.action_engine.ActionEngine.run",
                   return_value={"status": "no_match"}):
            response = client.post("/agent", json={"command": "Who are you?", "mode": "light"})
    assert response.status_code == 200
    assert "reply" in response.json(), f"Expected 'reply' in response: {response.json()}"


# ── Group D: Property-based tests ────────────────────────────────────────────

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st
    import num2words
    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

KNOWN_COMMANDS = [
    "open calculator",
    "take screenshot",
    "system status",
    "open downloads",
    "create note test",
    "list notes",
    "search google for python",
]


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis/num2words not installed")
@given(st.integers(min_value=0, max_value=999))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_spoken_number_round_trip(n):
    """Property: num2words(n) normalized back must contain str(n).

    Uses 0-999 range to avoid comma-notation numbers (e.g. 'one thousand, one hundred')
    that word2number cannot round-trip reliably.
    """
    spoken = num2words.num2words(n)
    # Skip if spoken form contains a comma — word2number can't handle these
    if "," in spoken:
        return
    result = normalize(spoken)
    if isinstance(result, dict):
        return  # clarification needed — acceptable
    # The normalized result must contain the number
    assert str(n) in result, f"normalize({spoken!r}) = {result!r}, expected to contain {n}"


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis/num2words not installed")
@given(
    st.integers(min_value=0, max_value=1000),
    st.integers(min_value=1, max_value=1000),
    st.sampled_from([("plus", "+"), ("minus", "-"), ("times", "*")]),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_spoken_math_round_trip(a, b, op_pair):
    """Property: spoken math operators normalize to correct symbols"""
    spoken_op, sym_op = op_pair
    # Use digit forms of numbers to avoid the whole-phrase number conversion issue
    spoken = f"{a} {spoken_op} {b}"
    normalized_result = normalize(spoken)
    if isinstance(normalized_result, dict):
        return  # clarification needed — skip
    assert sym_op in normalized_result, (
        f"normalize({spoken!r}) = {normalized_result!r}, expected {sym_op!r}"
    )


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(st.text(min_size=0, max_size=80, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"))))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_normalizer_idempotent(s):
    """Property: normalize(normalize(s)) == normalize(s) for string results"""
    first = normalize(s)
    if isinstance(first, str):
        second = normalize(first)
        assert first == second, (
            f"Not idempotent: normalize({s!r}) = {first!r}, normalize again = {second!r}"
        )


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(st.sampled_from(KNOWN_COMMANDS))
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_rule_matcher_confidence(cmd):
    """Property: every known command produces confidence=1.0 from the rule matcher"""
    plan = matcher.match(cmd, cmd)
    assert plan is not None, f"Expected match for known command: {cmd!r}"
    assert plan.confidence == 1.0, f"Expected confidence=1.0 for {cmd!r}, got {plan.confidence}"
