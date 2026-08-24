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

    def run(self, req: CommandRequest, *, confirmed: bool = False) -> dict:
        from jarvis_core.config import (
            ACTION_ENGINE_ENABLED, DANGEROUS_WORDS, CONFIDENCE_THRESHOLD,
            ENGINE_MAX_STEPS, ENGINE_USE_OLLAMA_FALLBACK,
        )

        if not ACTION_ENGINE_ENABLED:
            return {"status": "disabled", "message": "Action engine is disabled."}

        raw = req.command.strip()
        cmd_lower = raw.lower()

        # 1. DANGEROUS_WORDS pre-filter
        if not confirmed and any(word in cmd_lower for word in DANGEROUS_WORDS):
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
        from jarvis_core.intent_classifier import classify
        if classify(normalized, raw) == "conversational_query":
            return {"status": "no_match", "message": "Conversational request."}

        # 3. Rule match
        from jarvis_core.rule_matcher import matcher
        plan: Optional[ActionPlan] = matcher.match(normalized, raw)
        direct_execution = plan is not None
        if direct_execution:
            from jarvis_core.model_router import router
            router.record_direct("task", "Matched a verified Jarvis tool rule.")

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

        # 5. Confidence gate
        if plan.clarification_needed or plan.confidence < CONFIDENCE_THRESHOLD:
            msg = plan.clarification_prompt or "I could not safely determine the intended action. Please rephrase it."
            return {"status": "clarification_needed", "message": msg}

        # 6. Step count gate
        if len(plan.steps) > ENGINE_MAX_STEPS:
            return {"status": "error", "message": "Plan exceeds maximum allowed steps."}

        # Model plans can name a valid skill with an unsupported app/folder.
        # Reject that plan before execution so a user gets a clarification, not
        # a misleading partially-completed task card.
        parameter_error = self._validate_plan_parameters(plan)
        if parameter_error:
            return {"status": "clarification_needed", "message": parameter_error}

        # 7. Execute
        report = self.execute(plan, confirmed=confirmed)

        conf_id = getattr(report, "_confirmation_required", None)
        if conf_id:
            return {
                "status": "confirmation_required",
                "action_id": conf_id,
                "message": "This action needs your confirmation before Jarvis can continue.",
            }

        result_dict = report.model_dump()
        successful_steps = sum(1 for item in report.results if item.success)
        result_dict["status"] = (
            "done" if report.all_succeeded else "partial" if successful_steps else "failed"
        )
        from jarvis_core.model_router import router
        result_dict["routing"] = router.get_last()
        return result_dict

    def _validate_plan_parameters(self, plan: ActionPlan) -> Optional[str]:
        """Reject unsupported app/folder parameters before invoking a tool."""
        from jarvis_core.config import ALLOWED_APPS, ALLOWED_FOLDERS

        for step in plan.steps:
            if step.skill_id == "app.open":
                app_name = str(step.parameters.get("app_name", "")).strip().lower()
                if app_name not in ALLOWED_APPS:
                    supported = "Notepad, Calculator, Paint, Chrome, Edge, Task Manager"
                    return f"I don't recognise '{app_name or 'that'}' as a supported app. Try: {supported}."
            if step.skill_id in {"folder.open", "file.search"}:
                folder = str(step.parameters.get("folder_name", step.parameters.get("folder", ""))).strip().lower()
                if folder not in ALLOWED_FOLDERS:
                    return "I can search only Desktop, Documents, Downloads, Pictures, Notes, or Jarvis. Please choose one."
        return None

    def execute(self, plan: ActionPlan, *, confirmed: bool = False) -> ExecutionReport:
        from jarvis_core.skill_registry import registry, SkillNotFoundError
        results: list[ActionResult] = []

        for step in plan.steps:
            if step.requires_confirmation and not confirmed:
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
                output_dict = output if isinstance(output, dict) else {"value": output}
                step_success = output_dict.get("status") not in ("error", "failed")
                results.append(ActionResult(
                    step=step, success=step_success,
                    output=output_dict,
                    error=None if step_success else output_dict.get("message", "Step failed"),
                    verified=verified,
                ))
                if not step_success:
                    break
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
            "browser.youtube_play_latest",
            "browser.youtube_research",
            "browser.website_search", "folder.open", "notepad.type",
            "notepad.save", "app.focus",
        ):
            return True

        if sid == "calculator.calculate":
            if step.parameters.get("use_gui") and not out.get("gui_ok"):
                return False
            result = out.get("result") or out.get("value")
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

        if sid == "file.open_containing":
            return out.get("status") == "done" and bool(out.get("file"))

        if sid == "system.status":
            return "cpu_percent" in out

        return out.get("status") == "done"

    def _build_report(self, plan: ActionPlan, results: list[ActionResult]) -> ExecutionReport:
        if not results:
            return ExecutionReport(plan=plan, results=[], summary="No steps to execute.", all_succeeded=True)
        all_ok = all(r.success for r in results)
        succeeded = sum(1 for r in results if r.success)
        total = len(plan.steps)
        if succeeded == 0:
            summary = "Task failed before any step completed."
        else:
            summary = f"{succeeded}/{total} steps completed successfully."
        return ExecutionReport(
            plan=plan, results=results,
            summary=summary,
            all_succeeded=all_ok,
        )


engine = ActionEngine()
