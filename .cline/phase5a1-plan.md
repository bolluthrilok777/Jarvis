# Phase 5A-1 — AI Command Center & Multitask Queue

## Goal
Replace the single-command `/agent` flow with a multi-task queue that supports concurrent execution, risk classification, and a rich UI.

## Architecture

```
User Input (textarea)
  │
  ├── Enter / Execute button
  │     └── POST /tasks/submit  (splits multi-line → multiple tasks)
  │
  ├── Shift+Enter → newline (no submit)
  │
  └── Voice transcription → auto-submit if safe
        └── POST /tasks/submit

POST /tasks/submit
  └── IntentClassifier.classify()
        ├── "conversational_query" → POST /chat (existing AI flow)
        └── "executable_command" → TaskManager.create_task()
              └── Orchestrator.interpret()
                    ├── ActionEngine (existing) → ActionPlan
                    ├── RiskClassifier → SAFE/PROTECTED/CRITICAL
                    └── TaskManager.enqueue()
                          └── Scheduler dispatches based on concurrency limits

Task States: queued → interpreting → waiting_for_confirmation → running → completed/partially_completed/failed/cancelled
```

## Files to Create/Modify

### Create
1. `jarvis_core/task_manager.py` — Task queue, concurrency scheduler, CRUD
2. `jarvis_core/orchestrator.py` — Interprets input, classifies risk, manages lifecycle

### Complete (already exist as stubs)
3. `jarvis_core/task_models.py` — Already complete (Task, TaskStatus, RiskLevel, ConcurrencyGroup)
4. `jarvis_core/intent_classifier.py` — Already complete (classify function)
5. `jarvis_core/risk_classifier.py` — Already complete (classify_risk, classify_concurrency, build_interpreted_command)

### Modify
6. `jarvis_core/routes.py` — Add 6 task API endpoints
7. `jarvis_core/models.py` — Add TaskSubmitRequest, TaskConfirmRequest models
8. `ui.html` — Full rewrite for task-queue UI with Enter/Shift+Enter, task cards, auto-execute
9. `app.py` — Register new router if needed

### Test files (create, never delete existing)
10. `tests/test_task_manager.py` — Task queue, concurrency, state transitions
11. `tests/test_orchestrator.py` — Intent classification, risk, auto-execute, safety
12. `tests/test_task_routes.py` — API endpoint tests

## Implementation Order
1. Create plan file ✓
2. Complete `task_manager.py`
3. Complete `orchestrator.py`
4. Add request models to `models.py`
5. Add task routes to `routes.py`
6. Rewrite `ui.html`
7. Create test files
8. Run tests iteratively
9. Final test suite run
10. Git commit