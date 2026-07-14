"""
jarvis_core/action_engine.py — Phase 4.5 Action Engine Orchestrator
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Optional

from jarvis_core.action_models import ActionStep, ActionPlan, ActionResult, ExecutionReport
from jarvis_core.models import CommandRequest

logger = logging.getLogger("jarvis.engine")


class ActionEngine:

    def run(self, req: CommandRequest) -> dict:
        from jarvis_core.config import (
            ACTION_ENGINE_ENABLED, DANGEROUS_WORDS, CONFIDENCE_THRESHOLD,
            ENGINE_MAX_STEPS, ENGINE_USE_OLLAMA_FALLBACK,
        )

        if not ACTION_ENGINE_ENABLED:
            return {"status": "disabled", "message": "Action engine is disabled."}

        raw = req.command.strip()
        cmd_lower = raw.lower()

        # 1. DANGEROUS_WORDS pre-filter
        if any(word in cmd_lower for word in DANGEROUS_WORDS):
            import uuid, time as _t
            from jarvis_core.command_dispatcher import pending_actions
            action_id = str(uuid.uuid4())
            try:
                pending_actions[action_id] = {"command": raw, "created_at": _t.time()}
            except Exception as exc:
                logger.error("Failed to store pending action: %s", exc)
                return {"status": "error", "message": "Failed to store pending action."}
            logger.info("Dangerous command intercepted: %s", raw)
            return {
                "status": "confirmation_required",
                "action_id": action_id,
                "message": "This action needs your confirmation before Jarvis can continue.",
            }

        # 2. Normalize
        from jarvis_core.normalizer import normalize
        normalized = normalize(raw)
        if isinstance(normalized, dict):
            return normalized

        # 3. Rule match
        from jarvis_core.rule_matcher import matcher
        plan: Optional[ActionPlan] = matcher.match(normalized, raw)

        # 4. Ollama fallback
        if plan is None:
            if not ENGINE_USE_OLLAMA_FALLBACK:
                return {"status": "no_match", "message": "No rule matched and Ollama fallback is disabled."}
            from jarvis_core.ollama_planner import plan as ollama_plan
            result = ollama_plan(normalized, raw)
            if isinstance(result, dict):
                # Planner could not produce a valid ActionPlan (error, no skills matched,
                # service unavailable). Fall through to the legacy AI chat path.
                return {"status": "no_match", "message": result.get("message", "No plan found.")}
            plan = result
            # If the planner found a plan but confidence is too low, use AI chat instead.
            # We use a strict threshold here (1.0) for planner-generated plans because the
            # Ollama planner may hallucinate plans for conversational queries. Only rule-matched
            # plans (always confidence=1.0) should proceed to execution; planner plans route
            # to the legacy AI chat path for safety.
            if plan.confidence < 1.0:
                return {"status": "no_match", "message": "Low confidence — routing to AI chat."}

        # 5. Confidence gate
        if plan.clarification_needed or plan.confidence < CONFIDENCE_THRESHOLD:
            msg = plan.clarification_prompt or "I am not sure what you mean. Could you rephrase your command?"
            return {"status": "clarification_needed", "message": msg}

        # 6. Step count gate
        if len(plan.steps) > ENGINE_MAX_STEPS:
            return {"status": "error", "message": "Plan exceeds maximum allowed steps."}

        # 7. Execute
        report = self.execute(plan)

        conf_id = getattr(report, "_confirmation_required", None)
        if conf_id:
            return {
                "status": "confirmation_required",
                "action_id": conf_id,
                "message": "This action needs your confirmation before Jarvis can continue.",
            }

        result_dict = report.model_dump()
        result_dict["status"] = "done" if report.all_succeeded else "partial"
        return result_dict

    def execute(self, plan: ActionPlan) -> ExecutionReport:
        from jarvis_core.skill_registry import registry, SkillNotFoundError
        results: list[ActionResult] = []

        for step in plan.steps:
            if step.requires_confirmation:
                import uuid, time as _t
                from jarvis_core.command_dispatcher import pending_actions
                action_id = str(uuid.uuid4())
                pending_actions[action_id] = {
                    "command": f"skill:{step.skill_id}:{step.parameters}",
                    "created_at": _t.time(),
                }
                sentinel = ActionResult(
                    step=step, success=False,
                    output={"status": "confirmation_required", "action_id": action_id},
                    error="Requires confirmation", verified=False,
                )
                results.append(sentinel)
                report = self._build_report(plan, results)
                report._confirmation_required = action_id
                return report

            try:
                skill = registry.get_skill(step.skill_id)
                output = skill.executor(**step.parameters)
                verified = self._verify(step, output)
                results.append(ActionResult(
                    step=step, success=True,
                    output=output if isinstance(output, dict) else {"value": output},
                    verified=verified,
                ))
            except SkillNotFoundError as exc:
                results.append(ActionResult(step=step, success=False, error=f"Skill not found: {exc}"))
                break
            except Exception as exc:
                logger.error("Skill %s failed: %s", step.skill_id, exc)
                results.append(ActionResult(step=step, success=False, error=str(exc)))
                break

        return self._build_report(plan, results)

    def _verify(self, step: ActionStep, output) -> bool:
        sid = step.skill_id
        out = output if isinstance(output, dict) else {}

        if sid == "app.open":
            app_name = step.parameters.get("app_name", "").lower()
            from jarvis_core.config import ALLOWED_APPS
            exe = ALLOWED_APPS.get(app_name, "")
            exe_name = exe.lower() if exe else app_name
            import psutil
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                for proc in psutil.process_iter(["name"]):
                    try:
                        if proc.info["name"] and exe_name in proc.info["name"].lower():
                            return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                time.sleep(0.3)
            return False

        if sid in (
            "browser.open", "browser.google_search", "browser.youtube_search",
            "browser.website_search", "folder.open", "notepad.type",
            "notepad.save", "app.focus",
        ):
            return True

        if sid == "calculator.calculate":
            result = out.get("result")
            try:
                return result is not None and math.isfinite(float(result))
            except (TypeError, ValueError):
                return False

        if sid == "note.create":
            fp = out.get("file", "")
            return bool(fp) and Path(fp).exists()

        if sid == "system.screenshot":
            fp = out.get("file", "")
            return bool(fp) and Path(fp).exists()

        if sid in ("browser.read_page", "browser.website_screenshot"):
            return out.get("status") == "done"

        if sid == "file.search":
            return out.get("status") == "done"

        if sid == "system.status":
            return "cpu_percent" in out

        return out.get("status") == "done"

    def _build_report(self, plan: ActionPlan, results: list[ActionResult]) -> ExecutionReport:
        if not results:
            return ExecutionReport(plan=plan, results=[], summary="No steps to execute.", all_succeeded=True)
        all_ok = all(r.success for r in results)
        succeeded = sum(1 for r in results if r.success)
        total = len(plan.steps)
        return ExecutionReport(
            plan=plan, results=results,
            summary=f"{succeeded}/{total} steps completed successfully.",
            all_succeeded=all_ok,
        )


engine = ActionEngine()
