import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from jarvis_core.config import APP_NAME
from jarvis_core.logging_config import setup_logging
from jarvis_core.routes import router
from jarvis_core.voice_routes import router as voice_router
from jarvis_core.gmail_routes import router as gmail_router
from jarvis_core.architecture_routes import (
    router as architecture_router,
    ws_router as architecture_ws_router,
    page_router as architecture_page_router,
)
from jarvis_core.gmail_monitor import load_config as load_gmail_config

setup_logging()
load_gmail_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop Jarvis background workers with the application."""
    from jarvis_core.orchestrator import orchestrator
    from jarvis_core.telegram_service import telegram_service

    orchestrator.start_worker()
    telegram_service.start()
    from jarvis_core.feature_orchestrator import feature_orchestrator
    feature_orchestrator.start_worker()

    # Locked Jarvis architecture (Part 1): durable workflow engine. Restore
    # persisted non-terminal workflows into memory-visible state ONLY. Part 1
    # never launches Chrome/Kiro side effects on restart — later Parts decide
    # what to do next using get_recovery_plan().
    from jarvis_core.workflow_engine import workflow_engine
    try:
        restored = workflow_engine.restore_active_workflows()
        if restored:
            import logging as _logging
            _logging.getLogger("jarvis.workflow").info(
                "Restored %d active workflow(s) on startup", len(restored)
            )
    except Exception:
        import logging as _logging
        _logging.getLogger("jarvis.workflow").exception(
            "Workflow engine restore failed on startup"
        )

    # Part 4 — ChatGPT bootstrap worker (runs long browser ops off the request
    # thread). It does NOT auto-run anything on startup; it only drains
    # explicitly scheduled /start requests.
    from jarvis_core.workflow_bootstrap_service import workflow_bootstrap_service
    workflow_bootstrap_service.start_worker()
    # Part 5: Telegram command → SAME ChatGPT conversation. Its worker drains
    # commands routed from the existing Telegram bot; telegram_service.start()
    # also wires + starts it, this is idempotent single-flight.
    from jarvis_core.workflow_command_service import workflow_command_service
    workflow_command_service.start_worker()
    # Part 6: GPT copy capture + Telegram approval worker. telegram_service.start()
    # also wires + starts it; idempotent single-flight.
    from jarvis_core.workflow_approval_service import workflow_approval_service
    workflow_approval_service.start_worker()
    # Part 7: Kiro controller worker (approved prompt → exact Kiro project).
    from jarvis_core.workflow_kiro_service import workflow_kiro_service
    workflow_kiro_service.start_worker()
    # Continuous Kiro session-ownership monitor: detects a human session switch
    # while a workflow is KIRO_RUNNING/WAITING_PERMISSION and pauses fail-closed
    # (independent of Part 8 — never clicks permissions or captures results).
    workflow_kiro_service.start_ownership_monitor()
    # Part 5→6 recovery: any persisted GPT_OUTPUT_READY workflow that never had
    # its output captured (e.g. reached OUTPUT_READY before the handoff existed,
    # or a crash between transition and capture) is safely re-enqueued for Part 6
    # capture. Idempotent: already-captured workflows are skipped.
    try:
        recovered = workflow_approval_service.recover_pending_captures()
        if recovered:
            import logging as _logging
            _logging.getLogger("jarvis.approval").info(
                "Recovered %d pending GPT_OUTPUT_READY capture(s)", len(recovered))
    except Exception:
        pass
    yield
    workflow_kiro_service.stop_worker()
    workflow_approval_service.stop_worker()
    workflow_command_service.stop_worker()
    workflow_bootstrap_service.stop_worker()
    from jarvis_core.monitoring import monitoring

    telegram_service.stop()
    monitoring.stop()
    orchestrator.stop_worker()
    feature_orchestrator.stop_worker()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.include_router(router)
app.include_router(voice_router)
app.include_router(gmail_router)
# Part 2 — Architecture UI: REST + WebSocket + page (workflow engine visualiser)
app.include_router(architecture_router)
app.include_router(architecture_ws_router)
app.include_router(architecture_page_router)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
