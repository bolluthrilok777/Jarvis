"""
calculator_tools.py — Safe calculator evaluator and Windows Calculator GUI automation.

Two public functions:
  safe_calculate(expression)  — pure AST-based evaluation, no GUI.
  drive_calculator_gui(expr)  — type expression into Calculator window and press Enter.
"""
import ast
import math
import operator
import re
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

    try:
        import ctypes
        user32 = ctypes.windll.user32
        screen_left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        screen_top = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        screen_width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        screen_height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    except Exception:
        screen_left = screen_top = 0
        screen_width, screen_height = pyautogui.size()
    screen_right = screen_left + screen_width
    screen_bottom = screen_top + screen_height
    visible_wins = [
        candidate for candidate in wins
        if candidate.width > 0 and candidate.height > 0
        and candidate.left < screen_right and candidate.left + candidate.width > screen_left
        and candidate.top < screen_bottom and candidate.top + candidate.height > screen_top
    ]
    if not visible_wins:
        return failed("no visible Calculator window was found")

    # ── 2. Focus the window ─────────────────────────────────────────────────
    win = visible_wins[0]
    # Restore if minimized
    try:
        if hasattr(win, "isMinimized") and win.isMinimized:
            win.restore()
            _time.sleep(0.3)
    except Exception:
        pass
    try:
        win.activate()
    except Exception:
        # Windows 11 packaged Calculator can raise error code 0 here even
        # though a normal title-bar click can still focus it safely.
        pass
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
        try:
            pyautogui.click(win.left + win.width // 2, win.top + win.height // 2)
            _time.sleep(0.3)
        except Exception as exc:
            return failed(f"cannot focus Calculator controls ({exc})")
        active = gw.getActiveWindow()
        active_title = getattr(active, "title", "") if active else ""
        if "calculator" not in active_title.lower():
            try:
                import ctypes
                hwnd = int(getattr(win, "_hWnd", 0))
                if not hwnd:
                    return failed("Calculator window did not receive focus")
                user32 = ctypes.windll.user32
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.keybd_event(0x12, 0, 0, 0)  # ALT grants foreground permission
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 0x0002, 0)
                _time.sleep(0.3)
            except Exception as exc:
                return failed(f"cannot request Calculator foreground focus ({exc})")
            active = gw.getActiveWindow()
            active_title = getattr(active, "title", "") if active else ""
            if "calculator" not in active_title.lower():
                return failed("Calculator window did not receive focus")

    # ── 3. Clear any existing input ──────────────────────────────────────────
    try:
        pyautogui.press("escape", presses=2, interval=0.05)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
    except Exception as exc:
        return failed(f"cannot clear input ({exc})")

    # ── 4. Type the expression ──────────────────────────────────────────────
    try:
        expected = safe_calculate(expr)
    except Exception as exc:
        return failed(f"cannot verify expression ({exc})")

    previous_clipboard = None
    try:
        import pyperclip
        previous_clipboard = pyperclip.paste()
        pyperclip.copy(expr)
        pyautogui.hotkey("ctrl", "v")
        _time.sleep(0.4)
    except Exception as exc:
        return failed(f"typing error ({exc})")

    # ── 5. Press Enter ──────────────────────────────────────────────────────
    try:
        pyautogui.press("enter")
        _time.sleep(0.5)
        pyautogui.hotkey("ctrl", "c")
        _time.sleep(0.2)
        displayed = str(pyperclip.paste()).strip()
    except Exception as exc:
        return failed(f"enter key or result verification error ({exc})")
    finally:
        if previous_clipboard is not None:
            try:
                pyperclip.copy(previous_clipboard)
            except Exception:
                pass

    normalized_display = displayed.replace("\u2212", "-").replace(",", "")
    numbers = re.findall(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", normalized_display)
    if not numbers:
        return failed("Calculator did not expose a numeric result after Enter")
    actual = float(numbers[-1])
    if not math.isclose(actual, float(expected), rel_tol=1e-9, abs_tol=1e-9):
        return failed(
            f"displayed result {numbers[-1]} did not match expected result {expected}"
        )

    return {"status": "done", "gui_ok": True, "displayed_result": numbers[-1]}
