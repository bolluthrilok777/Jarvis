"""
calculator_tools.py — Safe calculator evaluator and Windows Calculator GUI automation.

Two public functions:
  safe_calculate(expression)  — pure AST-based evaluation, no GUI.
  drive_calculator_gui(expr)  — type expression into Calculator window and press Enter.
"""
import ast
import operator
import subprocess
import time as _time
from typing import Any, Dict, Union

ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _evaluate(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Only numbers are allowed.")

    if isinstance(node, ast.BinOp):
        operation = ALLOWED_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError("Unsupported mathematical operator.")
        return operation(_evaluate(node.left), _evaluate(node.right))

    if isinstance(node, ast.UnaryOp):
        operation = ALLOWED_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError("Unsupported mathematical operator.")
        return operation(_evaluate(node.operand))

    raise ValueError("Only mathematical expressions are allowed.")


def safe_calculate(expression: str) -> Union[int, float]:
    """Evaluate a mathematical expression safely using AST parsing (no eval)."""
    cleaned = (
        expression
        .replace("\u00d7", "*")
        .replace("\u00f7", "/")
        .replace("^", "**")
        .strip()
    )
    if len(cleaned) > 150:
        raise ValueError("Expression is too long.")
    parsed = ast.parse(cleaned, mode="eval")
    return _evaluate(parsed.body)


def _normalize_for_gui(expression: str) -> str:
    """Normalize expression for typing into Calculator GUI."""
    return (
        expression
        .replace("\u00d7", "*")
        .replace("\u00f7", "/")
        .replace("^", "**")
        .replace(" ", "")
        .strip()
    )


def drive_calculator_gui(expression: str) -> Dict[str, Any]:
    """
    Type *expression* into the Windows Calculator window and press Enter.

    This function does NOT compute the result — the caller should call
    safe_calculate() separately if a numeric result is needed.

    Returns a dict with keys:
      status       — "done" (always, even on failure — never raises)
      warning      — present only if something went wrong (str)
      gui_ok       — True if typing was actually performed, False otherwise

    Safety guarantees:
      - Never types into a random window.
      - Only types after confirming the Calculator window is active/focused.
      - If window cannot be found or focused, returns a warning and does NOT type.
    """
    def failed(reason: str) -> Dict[str, Any]:
        return {"status": "done", "gui_ok": False,
                "warning": f"Calculator GUI automation failed: {reason}"}

    expr = _normalize_for_gui(expression)
    if not expr:
        return failed("empty expression")
    if len(expr) > 200:
        return failed("expression too long")

    try:
        import pygetwindow as gw
        import pyautogui
    except ImportError as exc:
        return failed(f"missing dependency ({exc})")

    # ── 1. Locate or launch Calculator ──────────────────────────────────────
    wins = gw.getWindowsWithTitle("Calculator")
    if not wins:
        # Launch it
        try:
            subprocess.Popen("calc.exe", shell=True)
        except Exception as exc:
            return failed(f"cannot launch calc.exe ({exc})")
        # Wait up to 5 seconds for the window to appear
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            wins = gw.getWindowsWithTitle("Calculator")
            if wins:
                break
            _time.sleep(0.3)
        if not wins:
            return failed("window not found after 5s")

    # ── 2. Focus the window ─────────────────────────────────────────────────
    win = wins[0]
    # Restore if minimized
    try:
        if hasattr(win, "isMinimized") and win.isMinimized:
            win.restore()
            _time.sleep(0.3)
    except Exception:
        pass
    try:
        win.activate()
    except Exception as exc:
        return failed(f"cannot activate window ({exc})")
    _time.sleep(0.3)
    # Click only the title bar, then verify focus before sending keyboard input.
    try:
        cx = win.left + win.width // 2
        cy = win.top + 10
        pyautogui.click(cx, cy)
        _time.sleep(0.3)
    except Exception as exc:
        return failed(f"cannot focus window ({exc})")

    active = gw.getActiveWindow()
    active_title = getattr(active, "title", "") if active else ""
    if "calculator" not in active_title.lower():
        return failed("Calculator window did not receive focus")

    # ── 3. Clear any existing input ──────────────────────────────────────────
    try:
        pyautogui.press("escape")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
    except Exception as exc:
        return failed(f"cannot clear input ({exc})")

    # ── 4. Type the expression ──────────────────────────────────────────────
    try:
        try:
            import pyperclip
            previous_clipboard = pyperclip.paste()
            pyperclip.copy(expr)
            pyautogui.hotkey("ctrl", "v")
            pyperclip.copy(previous_clipboard)
        except Exception:
            pyautogui.write(expr, interval=0.05)
    except Exception as exc:
        return failed(f"typing error ({exc})")

    # ── 5. Press Enter ──────────────────────────────────────────────────────
    try:
        pyautogui.press("enter")
    except Exception as exc:
        return failed(f"enter key error ({exc})")

    return {"status": "done", "gui_ok": True}
