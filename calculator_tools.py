"""
calculator_tools.py — Safe calculator evaluator and Windows Calculator GUI automation.

Two public functions:
  safe_calculate(expression)  — pure AST-based evaluation, no GUI.
  drive_calculator_gui(expr)  — type expression into Calculator window and press Enter.

GUI automation is Windows-desktop-only. On headless nodes (Linux cloud VM)
drive_calculator_gui returns a structured ``unsupported_on_this_node``
response and the pure evaluator keeps working.
"""
import ast
import ctypes
import math
import operator
import re
import subprocess
import sys
import time as _time
from typing import Any, Dict, List, Optional, Union

from jarvis_core.desktop_capabilities import DESKTOP_AVAILABLE, IS_WINDOWS

if IS_WINDOWS:
    # ctypes.wintypes only exists on Windows; importing it unconditionally
    # would crash this module (and everything that imports it) on Linux.
    import ctypes.wintypes

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


# ── Low-level Win32 helpers ───────────────────────────────────────────────────

def _get_calc_pids() -> List[int]:
    """Return PIDs of all running Calculator processes."""
    pids: List[int] = []
    for name in ("CalculatorApp.exe", "calc.exe", "Calculator.exe"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                parts = line.strip().strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
        except Exception:
            pass
    return pids


def _is_calculator_running() -> bool:
    return bool(_get_calc_pids())


def _find_calculator_hwnd() -> Optional[int]:
    """
    Find the main visible Calculator window using two strategies:
      1. Title search (primary) — always runs. On Windows 10/11 the UWP
         Calculator top-level HWND belongs to ApplicationFrameHost.exe, so
         a title-based scan is the most reliable approach.
      2. Process-PID search (secondary) — used to widen the net for edge
         cases where the title hasn't appeared yet.
    Uses ctypes.wintypes.HWND (pointer-sized) to avoid 32-bit truncation
    on 64-bit Windows.
    """
    user32 = ctypes.windll.user32
    found: List[int] = []

    # Use proper 64-bit-safe types for the EnumWindows callback
    EnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )

    # ── Strategy 1: find by window title ─────────────────────────────────────
    def _by_title(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if "calculator" in buf.value.lower():
            # Accept both normal and minimized windows (IsIconic returns True
            # when minimized; GetWindowRect gives negative coords in that case)
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(_by_title), 0)

    # ── Strategy 2: find by process PID (catches early launch before title) ──
    if not found:
        # Include ApplicationFrameHost.exe — this is the UWP app host process
        # that owns the top-level HWND for all UWP apps on Win10/11
        calc_pids = set(_get_calc_pids())
        host_pids: List[int] = []
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ApplicationFrameHost.exe",
                 "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                parts = line.strip().strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        host_pids.append(int(parts[1]))
                    except ValueError:
                        pass
        except Exception:
            pass

        all_pids = calc_pids | set(host_pids)

        def _by_pid(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in all_pids:
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.right - rect.left) > 50 and (rect.bottom - rect.top) > 50:
                    found.append(hwnd)
            return True

        if all_pids:
            user32.EnumWindows(EnumProc(_by_pid), 0)

    if not found:
        return None

    # Prefer top-level windows (GetParent == 0) — ignore child/embedded panes
    top_level = [h for h in found if user32.GetParent(h) == 0]
    return top_level[0] if top_level else found[0]


def _standard_calculator_expression(expression: str) -> str:
    """Render a safe AST with parentheses for Calculator's left-to-right mode."""
    parsed = ast.parse(expression, mode="eval")
    symbols = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}

    def render(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return str(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            sign = "-" if isinstance(node.op, ast.USub) else "+"
            return f"({sign}{render(node.operand)})"
        if isinstance(node, ast.BinOp):
            symbol = symbols.get(type(node.op))
            if symbol is None:
                raise ValueError("This operation is not available in standard Calculator mode.")
            return f"({render(node.left)}{symbol}{render(node.right)})"
        raise ValueError("Only basic arithmetic is available in standard Calculator mode.")

    return render(parsed.body)


_CALCULATOR_OPERATOR_KEYS = {"+": 0x6B, "-": 0x6D, "*": 0x6A, "/": 0x6F}
_KEYEVENTF_KEYUP = 0x0002
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11


def _send_virtual_key(key: int, *, shift: bool = False, control: bool = False) -> None:
    """Send one focused-window key with Win32, including secondary monitors."""
    user32 = ctypes.windll.user32
    if shift:
        user32.keybd_event(_VK_SHIFT, 0, 0, 0)
    if control:
        user32.keybd_event(_VK_CONTROL, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, _KEYEVENTF_KEYUP, 0)
    if control:
        user32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)
    if shift:
        user32.keybd_event(_VK_SHIFT, 0, _KEYEVENTF_KEYUP, 0)


def _type_calculator_expression(expression: str) -> None:
    """Send only validated Calculator keys, independent of mouse position/layout."""
    for character in expression:
        if character.isdigit():
            _send_virtual_key(ord(character))
        elif character == ".":
            _send_virtual_key(0x6E)  # VK_DECIMAL
        elif character in _CALCULATOR_OPERATOR_KEYS:
            _send_virtual_key(_CALCULATOR_OPERATOR_KEYS[character])
        elif character == "(":
            _send_virtual_key(ord("9"), shift=True)
        elif character == ")":
            _send_virtual_key(ord("0"), shift=True)
        else:
            raise ValueError(f"Unsupported Calculator character: {character!r}")
        _time.sleep(0.04)


def _focus_hwnd(hwnd: int) -> None:
    """Restore (if minimized) and bring hwnd to foreground."""
    user32 = ctypes.windll.user32
    # Always restore first — handles minimized/iconified windows
    user32.ShowWindow(hwnd, 9)           # SW_RESTORE
    _time.sleep(0.2)
    user32.keybd_event(0x12, 0, 0, 0)   # ALT down (grants foreground permission)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT up
    _time.sleep(0.3)


def _launch_calculator_once() -> bool:
    """Launch Calculator only if not already running. Returns True if running."""
    if _is_calculator_running():
        return True
    try:
        subprocess.Popen("calc.exe", shell=True)
    except Exception:
        return False
    deadline = _time.monotonic() + 6.0
    while _time.monotonic() < deadline:
        if _is_calculator_running():
            return True
        _time.sleep(0.25)
    return False



def _close_duplicate_calc_windows(keep_hwnd: int) -> None:
    """Close all visible Calculator windows except keep_hwnd."""
    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    duplicates: List[int] = []

    def _cb(h: int, _: int) -> bool:
        if h == keep_hwnd:
            return True
        if not user32.IsWindowVisible(h) or user32.GetParent(h) != 0:
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        if "calculator" in buf.value.lower():
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            if (rect.right - rect.left) > 50 and (rect.bottom - rect.top) > 50:
                duplicates.append(h)
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    for dup in duplicates:
        try:
            user32.PostMessageW(dup, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass



# ── Public GUI driver ─────────────────────────────────────────────────────────

def drive_calculator_gui(expression: str) -> Dict[str, Any]:
    """
    Type *expression* into the Windows Calculator window and press Enter.

    - Never opens a second Calculator if one is already running.
    - Finds the window by process PID via EnumWindows (not window title),
      so it works with the UWP Store Calculator even before title appears.
    - Closes any duplicate Calculator windows.
    - Returns {status, gui_ok, warning?} — never raises.
    """
    def failed(reason: str) -> Dict[str, Any]:
        return {"status": "done", "gui_ok": False,
                "warning": f"Calculator GUI automation failed: {reason}"}

    # Headless nodes have no Calculator window to drive. The pure evaluator
    # above still returned the correct numeric result; report cleanly here.
    if not DESKTOP_AVAILABLE or not IS_WINDOWS:
        refusal = {
            "status": "unsupported_on_this_node",
            "feature": "calculator.gui",
            "message": (
                "Calculator GUI automation requires a Windows desktop "
                "session, which is not available on this node. Use plain "
                "calculate instead; all other features remain operational."
            ),
            "desktop_available": False,
        }
        return refusal

    expr = _normalize_for_gui(expression)
    if not expr:
        return failed("empty expression")
    if len(expr) > 200:
        return failed("expression too long")

    try:
        import pyperclip
    except ImportError as exc:
        return failed(f"missing dependency ({exc})")

    # ── 1. Ensure Calculator is running (never launch a duplicate) ────────────
    if not _launch_calculator_once():
        return failed("could not launch Calculator")

    # ── 2. Find the window — restore from taskbar if minimized ────────────────
    hwnd: Optional[int] = None
    deadline = _time.monotonic() + 8.0
    while _time.monotonic() < deadline:
        hwnd = _find_calculator_hwnd()
        if hwnd:
            # Restore immediately if minimized so it becomes interactable
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _time.sleep(0.5)
            break
        _time.sleep(0.3)
    if not hwnd:
        return failed("Calculator window not found after 8s")

    # ── 3. Close duplicate Calculator windows ─────────────────────────────────
    _close_duplicate_calc_windows(hwnd)
    _time.sleep(0.1)

    # ── 4. Focus the window ───────────────────────────────────────────────────
    _focus_hwnd(hwnd)

    # ── 5. Clear any existing input ───────────────────────────────────────────
    try:
        _send_virtual_key(0x1B)  # Escape
        _send_virtual_key(0x1B)
        _send_virtual_key(ord("A"), control=True)
        _send_virtual_key(0x2E)  # Delete
        _time.sleep(0.1)
    except Exception as exc:
        return failed(f"cannot clear input ({exc})")

    # ── 6. Type expression as keyboard input ─────────────────────────────────
    # Calculator does not reliably accept pasted formulas on every Windows
    # build. Key events are slower but consistently update the focused app.
    try:
        expected = safe_calculate(expr)
        calculator_expr = _standard_calculator_expression(expr)
    except Exception as exc:
        return failed(f"cannot verify expression ({exc})")

    previous_clipboard = None
    try:
        previous_clipboard = pyperclip.paste()
        _type_calculator_expression(calculator_expr)
        _time.sleep(0.25)
    except Exception as exc:
        return failed(f"typing error ({exc})")

    # ── 7. Press Enter and verify result ─────────────────────────────────────
    numbers: List[str] = []
    try:
        _send_virtual_key(0x0D)  # Enter
        deadline2 = _time.monotonic() + 2.5
        while _time.monotonic() < deadline2:
            _time.sleep(0.25)
            _send_virtual_key(ord("C"), control=True)
            _time.sleep(0.1)
            displayed = str(pyperclip.paste()).strip()
            normalized_display = displayed.replace("\u2212", "-").replace(",", "")
            numbers = re.findall(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", normalized_display)
            if numbers and math.isclose(
                float(numbers[-1]), float(expected), rel_tol=1e-9, abs_tol=1e-9
            ):
                break
    except Exception as exc:
        return failed(f"enter key or result verification error ({exc})")
    finally:
        if previous_clipboard is not None:
            try:
                pyperclip.copy(previous_clipboard)
            except Exception:
                pass

    if not numbers:
        return failed("Calculator did not expose a numeric result after Enter")
    actual = float(numbers[-1])
    if not math.isclose(actual, float(expected), rel_tol=1e-9, abs_tol=1e-9):
        return failed(
            f"displayed result {numbers[-1]} did not match expected {expected}"
        )

    return {"status": "done", "gui_ok": True, "displayed_result": numbers[-1]}
