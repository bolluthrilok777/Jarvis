# Jarvis Local Agent — Project Status

**Phase:** 5A-1 — AI Command Center & Multitask Queue  
**Last updated:** 2026-07-15  
**Status:** Stable, all routes verified

---

## Architecture

Jarvis is a local Windows AI assistant built on FastAPI + Ollama. After Phase 2 modularization, the project uses a layered package structure with a thin entry point. Phase 4 adds a voice subsystem as a parallel branch.

```
app.py  (entry point — ~14 lines, wiring only)
  └── jarvis_core/routes.py  (route handlers + Phase 5A-1 task API)
        ├── jarvis_core/orchestrator.py  (Phase 5A-1: task lifecycle, risk, concurrency)
        │     ├── jarvis_core/intent_classifier.py  (intent detection)
        │     ├── jarvis_core/risk_classifier.py  (SAFE/PROTECTED/CRITICAL)
        │     ├── jarvis_core/task_manager.py  (task CRUD, concurrency slots)
        │     ├── jarvis_core/task_models.py  (Task, TaskStatus, RiskLevel enums)
        │     └── jarvis_core/action_engine.py  (Phase 4.5 action pipeline)
        ├── jarvis_core/command_dispatcher.py  (all command routing)
        ├── jarvis_core/ollama_service.py  (Ollama API + error handling)
        ├── jarvis_core/system_tools.py  (screenshots, system status)
        ├── jarvis_core/file_tools.py  (safe file reading)
        └── jarvis_core/app_tools.py  (apps, folders, notes, search)
  └── jarvis_core/voice_routes.py  (3 voice endpoints)
        └── jarvis_core/voice_service.py  (lazy Whisper model, transcription)

browser_tools.py   (Playwright — standalone)
calculator_tools.py  (AST calculator — standalone)
```

**Import rule:** `config.py` is the only leaf module. No cycles exist.

---

## Project Structure

```
C:\Jarvis\
  app.py                    Entry point (~14 lines)
  run_jarvis.bat            Launcher with health check and auto browser
  requirements.txt          Pinned direct dependencies
  .gitignore
  ui.html                   Web interface (served via FileResponse)
  browser_tools.py          Playwright webpage reading and screenshots
  calculator_tools.py       AST-safe calculator + GUI automation
  jarvis_core\
    __init__.py
    config.py               All shared constants
    models.py               Pydantic request models
    logging_config.py       Rotating file logger setup
    ollama_service.py       Ollama API client with structured errors
    system_tools.py         System status and desktop screenshots
    file_tools.py           Safe file reading with path traversal protection
    app_tools.py            App/folder launching, notes, Google search
    command_dispatcher.py   Command routing, pending actions, confirmation
    routes.py               FastAPI APIRouter with Phase 5A-1 task API
    voice_service.py        Lazy-loading Whisper, validation, transcription
    voice_routes.py         POST /voice/transcribe, GET /voice/status, POST /voice/unload
    action_models.py        Phase 4.5: ActionStep, ActionPlan, ActionResult, ExecutionReport
    action_engine.py        Phase 4.5: Action pipeline orchestrator
    skill_registry.py       Phase 4.5: 18 registered skills with executors
    rule_matcher.py         Phase 4.5: 19 deterministic regex rules
    normalizer.py           Phase 4.5: Filler removal, spoken-operator conversion
    ollama_planner.py       Phase 4.5: Ollama-based plan generation
    intent_classifier.py    Phase 5A-1: Intent detection (command vs conversational)
    risk_classifier.py      Phase 5A-1: SAFE/PROTECTED/CRITICAL risk classification
    task_manager.py         Phase 5A-1: Task CRUD, concurrency slots, duplicate prevention
    task_models.py          Phase 5A-1: Task, TaskStatus, RiskLevel, ConcurrencyGroup enums
    orchestrator.py         Phase 5A-1: Task lifecycle orchestration
  tests\
    __init__.py
    test_imports.py         Test 1: import validation
    test_routes.py          Tests 2-8: route behaviors
    test_tools.py           Tests 9-13: tools and calculator
    test_security.py        Tests 14-22: security and path blocking
    test_voice.py           Phase 4 voice tests (29 unit + 5 property = 34 tests)
    test_action_engine.py   Phase 4.5: 21 unit + 4 property tests
    test_orchestrator.py    Phase 5A-1: Orchestrator tests (23 tests)
    test_task_manager.py    Phase 5A-1: Task manager tests (24 tests)
    test_task_routes.py     Phase 5A-1: Task API route tests (10 tests)
  logs\                     Rotating logs (gitignored)
  notes\                    User notes (gitignored)
  screenshots\              Desktop screenshots (gitignored)
  web_screenshots\          Website screenshots (gitignored)
  models\
    whisper\                Whisper base.en model weights (downloaded on first use)
  .venv\                    Python virtual environment (gitignored)
```

---

## Working Features

- **FastAPI server** at http://127.0.0.1:8000
- **Ollama AI integration** — gemma2:2b (light), qwen2.5:7b (heavy)
- **System status** — CPU, RAM, disk, process count
- **Desktop screenshots** via PIL ImageGrab
- **Open applications** — allowlisted: notepad, calculator, paint, cmd, powershell, task manager, explorer, chrome, edge
- **Open folders** — allowlisted: downloads, desktop, documents, pictures, jarvis, notes, screenshots
- **Open websites** via webbrowser module
- **Google search** with proper URL encoding (urllib.parse.quote_plus)
- **Create and list notes** — timestamped .txt files in notes/
- **Safe file reading** — allowed extensions and roots, path traversal protection
- **List Ollama models**
- **AST-safe calculator** — no eval(), arithmetic only
- **Calculator GUI automation** — opens calc.exe and types expression
- **Playwright webpage reading** — headless Chromium
- **Playwright website screenshots**
- **AI fallback** — unmatched commands go to Ollama
- **Dangerous command confirmation** — delete/payment/etc require explicit confirmation; execution of protected actions is not implemented in the current safe version
- **Pending action expiry** — confirmations expire after 300 seconds

---

## Voice Input (Phase 4)

Jarvis supports safe, local push-to-talk voice input with no cloud dependency.

### How It Works
1. User clicks **🎙 Start Listening** in the web UI
2. Browser requests microphone permission (`getUserMedia`)
3. `MediaRecorder` captures audio in OGG/Opus format (max 15 seconds)
4. User clicks stop manually, or recording auto-stops at 15 seconds
5. Browser sends audio to `POST /voice/transcribe` (multipart upload)
6. `faster-whisper` transcribes locally using the `base.en` model
7. Transcribed text is inserted into the command textarea
8. User reviews and clicks **Execute** manually — **never auto-executed**

### Transcription Engine
- **Model:** `base.en` (English-optimized, ~145 MB)
- **Backend:** `faster-whisper` with CTranslate2
- **Device:** CPU only (no CUDA required)
- **Precision:** INT8 (optimized for low RAM)
- **Audio format:** OGG/Opus (from browser MediaRecorder)

### Memory & Performance
- Whisper model loads lazily — zero startup penalty
- Model stays loaded for 120 seconds of inactivity, then unloads automatically
- RAM usage: ~150–250 MB when model is loaded, 0 additional when idle
- First transcription: ~2–5 seconds (model load) + ~1–3 seconds (transcription)
- Subsequent transcriptions: ~1–3 seconds
- Use `POST /voice/unload` to free RAM immediately

### Privacy & Safety
- All audio processing happens on-device
- No audio sent to Ollama, no cloud API calls
- Temporary audio files deleted immediately after transcription
- Microphone only accessed after explicit user click
- No background listening, no wake word, no always-on mode
- Transcribed text placed in textarea only — never auto-executed

### Troubleshooting
| Problem | Solution |
|---------|----------|
| Microphone permission denied | Check browser microphone permissions for localhost |
| No microphone detected | Connect a microphone and refresh |
| First transcription slow | Expected — model downloads ~145 MB on first use |
| Model download fails | Check internet; model stored at `C:\Jarvis\models\whisper\` |
| High memory usage | Call `POST /voice/unload` or wait 120s for auto-unload |
| Transcription empty | Speak closer to microphone; check VAD filter |
| `audio/webm not supported` | Use Chrome or Edge with OGG codec support |

---

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | / | App info, model names, UI URL |
| GET | /health | Ollama status, system stats |
| GET | /ui | Web interface (ui.html via FileResponse) |
| POST | /chat | Direct AI chat (ChatRequest) |
| POST | /agent | Command dispatch (CommandRequest) |
| POST | /command | Alias for /agent |
| POST | /confirm-action | Confirm or cancel a pending action |
| POST | /voice/transcribe | Upload audio file, get transcription |
| GET  | /voice/status     | Voice model status (loaded/unloaded) |
| POST | /voice/unload     | Unload voice model from memory |
| POST | /tasks/submit | Submit one or more command lines (multi-line = separate tasks) |
| GET  | /tasks | List all visible tasks |
| GET  | /tasks/{task_id} | Get a single task by ID |
| POST | /tasks/{task_id}/cancel | Cancel a task |
| POST | /tasks/{task_id}/confirm | Confirm or reject a task |
| POST | /tasks/clear-completed | Hide all completed/failed/cancelled tasks |

---

## Supported Command Prefixes

| Command | Action |
|---------|--------|
| `system status` | CPU/RAM/disk report |
| `take screenshot` | Capture desktop |
| `open app <name>` | Launch allowlisted app |
| `open folder <name>` | Open Explorer to folder |
| `open notepad` / `open calculator` | Direct shortcuts |
| `open website <url>` | Open in browser |
| `open <domain.com>` | Auto-detect URL |
| `search google for <query>` | Google search |
| `google <query>` | Google search shorthand |
| `create note <text>` | Save note to notes/ |
| `list notes` | List recent notes |
| `read file <path>` | Read allowed text file |
| `read webpage <url>` | Read webpage via Playwright |
| `website screenshot <url>` | Screenshot via Playwright |
| `list models` | List Ollama models |
| *(anything else)* | AI fallback via Ollama |

---

## AI Models

| Model | When used |
|-------|-----------|
| gemma2:2b | Default (mode=light) |
| qwen2.5:7b | mode=heavy / mode=deep / mode=reasoning |

**Settings:** keep_alive=30s, num_ctx=2048, temperature=0.4

---

## Dependencies

See `requirements.txt` for pinned versions.

| Package | Purpose |
|---------|---------|
| fastapi | Web framework |
| uvicorn[standard] | ASGI server |
| pydantic | Request validation |
| requests | HTTP client (Ollama API) |
| psutil | System metrics |
| Pillow | Desktop screenshots |
| playwright | Browser automation |
| pyautogui | Calculator GUI automation |
| faster-whisper | Local speech-to-text (Whisper base.en, CPU INT8) |
| python-multipart | Multipart form upload parsing |

---

## Starting Jarvis

**Option 1 — Launcher (recommended):**
```
C:\Jarvis\run_jarvis.bat
```
Checks prerequisites, starts server, polls health, opens browser automatically.

**Option 2 — Manual:**
```
cd C:\Jarvis
.venv\Scripts\activate
uvicorn app:app --host 127.0.0.1 --port 8000
```
Then open http://127.0.0.1:8000/ui

---

## Running Tests

```
cd C:\Jarvis
.venv\Scripts\activate
python -m pytest tests/ -v
```

Expected: 147 passed (Phase 5A-1: 66 Phase 5A-1 tests + 81 pre-existing tests)

---

## Safety Controls

- **No eval()** — calculator uses AST parsing only
- **App allowlist** — only known-safe executables can be launched
- **Folder allowlist** — only known-safe folders can be opened
- **File root restriction** — reads only from C:\Jarvis, Desktop, Documents, Downloads
- **Path traversal protection** — Path.resolve() applied before all checks
- **File extension restriction** — only .txt .md .py .json .csv .log
- **File size limit** — max 1 MB
- **URL scheme check** — only http/https (browser_tools)
- **Browser cleanup** — Playwright always closes browser in finally block
- **Dangerous command confirmation** — risky commands require explicit confirmation
- **Confirmation expiry** — pending actions expire after 300 seconds
- **Localhost only** — never exposed beyond 127.0.0.1
- **No audio transmission** — voice never sent externally
- **Push-to-talk only** — microphone accessed only on explicit click
- **Temp audio cleanup** — recordings deleted after transcription (success or failure)
- **No auto-execution** — transcribed text goes to textarea only, never auto-submitted
- **Path traversal protection** — uploaded filenames never used, random temp names generated
- **MIME validation** — only OGG/WAV/OctetStream accepted
- **Size limit** — uploads capped at 10 MB

---

## Known Limitations

- No persistent memory — state resets on server restart
- Single-user — no authentication
- Windows only — uses Windows paths and executables
- Requires Ollama running locally for AI features
- Calculator GUI automation requires Windows Calculator app
- Playwright requires Chromium browser installed (`playwright install chromium`)
- Task queue executes sequentially (no parallel execution in Phase 5A-1)
- No persistent task storage — tasks lost on server restart
- Duplicate submission prevention is best-effort (based on text hash)

---

## Phase 4.5 — Universal Natural-Language Action Engine

**Added:** 2026  
**Status:** Complete — 82/82 tests passing

---

## Phase 5A-1 — AI Command Center & Multitask Queue

**Added:** 2026-07-15  
**Status:** Complete — 66/66 Phase 5A-1 tests passing (147/148 total)

### What Changed

Phase 5A-1 adds a structured task queue with risk-based execution, concurrency control, and a full task lifecycle API. Users can submit multiple commands at once, review protected actions before execution, and track task status through the UI.

### New Modules

| File | Purpose |
|------|---------|
| `jarvis_core/intent_classifier.py` | Classifies input as `conversational_query` or `executable_command` |
| `jarvis_core/risk_classifier.py` | Classifies plans as SAFE, PROTECTED, or CRITICAL |
| `jarvis_core/task_models.py` | Enums: TaskStatus, RiskLevel, ConcurrencyGroup |
| `jarvis_core/task_manager.py` | Task CRUD, concurrency slots, duplicate prevention |
| `jarvis_core/orchestrator.py` | Task lifecycle: submit, confirm, cancel, execute |
| `tests/test_orchestrator.py` | 23 orchestrator tests |
| `tests/test_task_manager.py` | 24 task manager tests |
| `tests/test_task_routes.py` | 10 task API route tests |

### Key Behaviors

- **Enter** submits the command (single-line)
- **Shift+Enter** inserts a new line (multi-line input)
- **Multiple non-empty lines** create separate tasks
- **Blank lines** are ignored
- **Execute button and Enter** use the same submission function
- **SAFE commands** auto-execute (queued immediately)
- **PROTECTED commands** wait for user confirmation
- **CRITICAL commands** are blocked for safety
- **Conversational queries** route to AI chat (not task queue)
- **Duplicate submissions** are prevented (same text hash within a session)
- **Desktop-focus concurrency** limited to one slot
- **No arbitrary shell execution** — all commands go through the action engine
- **No AI-generated Python or PowerShell** is executed

### Task Lifecycle

```
created → interpreting → queued → running → completed
                                    ↓
                              waiting_for_confirmation → queued → running → completed
                                    ↓
                                  cancelled
```

### Safety Controls

- NEVER_EXECUTE_PATTERNS block dangerous Python/PowerShell patterns
- PROTECTED_KEYWORDS require user confirmation
- CRITICAL_KEYWORDS are blocked immediately
- `_validate_plan()` rejects unsafe AI-generated plans
- Concurrency slots prevent resource exhaustion
- Duplicate submission prevention prevents accidental re-submission

### Test Results

```
147 passed in ~107s
  66  Phase 5A-1 tests (orchestrator + task_manager + task_routes)
  81  pre-existing tests (imports, routes, tools, security, voice, action_engine)
```
