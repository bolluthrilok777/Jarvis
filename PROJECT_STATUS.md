# Jarvis Local Agent — Project Status

**Phase:** 2 — Stabilized & Modularized  
**Last updated:** 2025  
**Status:** Stable, all routes verified

---

## Architecture

Jarvis is a local Windows AI assistant built on FastAPI + Ollama. After Phase 2 modularization, the project uses a layered package structure with a thin entry point.

```
app.py  (entry point — ~14 lines, wiring only)
  └── jarvis_core/routes.py  (7 route handlers, no business logic)
        ├── jarvis_core/command_dispatcher.py  (all command routing)
        ├── jarvis_core/ollama_service.py  (Ollama API + error handling)
        ├── jarvis_core/system_tools.py  (screenshots, system status)
        ├── jarvis_core/file_tools.py  (safe file reading)
        └── jarvis_core/app_tools.py  (apps, folders, notes, search)

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
    routes.py               FastAPI APIRouter with 7 routes
  tests\
    __init__.py
    test_imports.py         Test 1: import validation
    test_routes.py          Tests 2-8: route behaviors
    test_tools.py           Tests 9-13: tools and calculator
    test_security.py        Tests 14-22: security and path blocking
  logs\                     Rotating logs (gitignored)
  notes\                    User notes (gitignored)
  screenshots\              Desktop screenshots (gitignored)
  web_screenshots\          Website screenshots (gitignored)
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
- **Dangerous command confirmation** — delete/payment/etc require explicit confirmation
- **Pending action expiry** — confirmations expire after 300 seconds

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

Expected: 22 passed, 0 failed (1 may be skipped if no internet)

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

---

## Known Limitations

- No persistent memory — state resets on server restart
- Single-user — no authentication
- Windows only — uses Windows paths and executables
- Requires Ollama running locally for AI features
- Calculator GUI automation requires Windows Calculator app
- Playwright requires Chromium browser installed (`playwright install chromium`)
