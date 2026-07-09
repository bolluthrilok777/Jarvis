import ast
import operator
import subprocess
import time

import pyautogui


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

        return operation(
            _evaluate(node.left),
            _evaluate(node.right)
        )

    if isinstance(node, ast.UnaryOp):
        operation = ALLOWED_OPERATORS.get(type(node.op))

        if operation is None:
            raise ValueError("Unsupported mathematical operator.")

        return operation(_evaluate(node.operand))

    raise ValueError("Only mathematical expressions are allowed.")


def safe_calculate(expression: str):
    cleaned = (
        expression
        .replace("×", "*")
        .replace("÷", "/")
        .replace("^", "**")
        .strip()
    )

    if len(cleaned) > 150:
        raise ValueError("Expression is too long.")

    parsed = ast.parse(cleaned, mode="eval")

    return _evaluate(parsed.body)


def open_calculator_and_calculate(expression: str):
    result = safe_calculate(expression)

    calculator_expression = (
        expression
        .replace("×", "*")
        .replace("÷", "/")
        .replace("^", "**")
        .strip()
    )

    subprocess.Popen("calc.exe")

    time.sleep(2)

    pyautogui.write(
        calculator_expression,
        interval=0.05
    )

    pyautogui.press("enter")

    return result