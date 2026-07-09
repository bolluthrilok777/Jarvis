from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import subprocess
import os
import uuid
import time
import webbrowser
import pathlib
import datetime
import psutil
from PIL import ImageGrab
from calculator_tools import open_calculator_and_calculate
from browser_tools import read_webpage, screenshot_webpage

APP_NAME = "Jarvis Local Agent - Phase 2"
OLLAMA_URL = "http://localhost:11434"

LIGHT_MODEL = "gemma2:2b"
HEAVY_MODEL = "qwen2.5:7b"

BASE_DIR = pathlib.Path("C:/Jarvis")
NOTES_DIR = BASE_DIR / "notes"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

NOTES_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)

app = FastAPI(title=APP_NAME)
pending_actions = {}

SYSTEM_PROMPT = """
You are Jarvis, a local AI assistant running on a Windows laptop.
Use short and practical answers.
Default mode is lightweight because the laptop has 16GB RAM.
Use heavy reasoning only when requested.
Ask confirmation before dangerous actions like deleting files, sending emails, submitting forms, booking slots, payments, or server changes.
"""

class ChatRequest(BaseModel):
    message: str
    mode: str = "light"

class CommandRequest(BaseModel):
    command: str
    mode: str = "light"

class ConfirmRequest(BaseModel):
    action_id: str
    confirm: bool

def select_model(mode: str):
    if mode.lower() in ["heavy", "deep", "reasoning"]:
        return HEAVY_MODEL
    return LIGHT_MODEL

def ask_ollama(message: str, mode: str = "light"):
    model = select_model(mode)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message}
        ],
        "stream": False,
        "keep_alive": "30s",
        "options": {
            "num_ctx": 2048,
            "temperature": 0.4
        }
    }

    response = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    response.raise_for_status()
    return response.json()["message"]["content"], model

def get_system_status():
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("C:/")

    return {
        "cpu_percent": cpu,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_percent": memory.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "running_processes": len(psutil.pids())
    }

def take_screenshot():
    filename = datetime.datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
    file_path = SCREENSHOT_DIR / filename

    image = ImageGrab.grab()
    image.save(file_path)

    return str(file_path)

def open_known_app(app_name: str):
    apps = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "paint": "mspaint.exe",
        "cmd": "cmd.exe",
        "command prompt": "cmd.exe",
        "powershell": "powershell.exe",
        "task manager": "taskmgr.exe",
        "explorer": "explorer.exe",
        "chrome": "chrome.exe",
        "edge": "msedge.exe"
    }

    app_name = app_name.lower().strip()

    if app_name in apps:
        subprocess.Popen(apps[app_name])
        return True, f"Opened {app_name}."

    return False, f"App not found in allowed list: {app_name}"

def open_known_folder(folder_name: str):
    home = pathlib.Path.home()

    folders = {
        "downloads": home / "Downloads",
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "pictures": home / "Pictures",
        "jarvis": BASE_DIR,
        "notes": NOTES_DIR,
        "screenshots": SCREENSHOT_DIR
    }

    folder_name = folder_name.lower().strip()

    if folder_name in folders:
        subprocess.Popen(f'explorer "{folders[folder_name]}"')
        return True, f"Opened {folder_name} folder."

    return False, f"Folder not found: {folder_name}"

def read_text_file(file_path_text: str):
    file_path = pathlib.Path(file_path_text.strip().strip('"'))

    if not file_path.exists():
        return {
            "status": "error",
            "message": "File not found."
        }

    allowed_extensions = [".txt", ".md", ".py", ".json", ".csv", ".log"]

    if file_path.suffix.lower() not in allowed_extensions:
        return {
            "status": "blocked",
            "message": "For safety, Jarvis can read only txt, md, py, json, csv, and log files in this version."
        }

    if file_path.stat().st_size > 1024 * 1024:
        return {
            "status": "blocked",
            "message": "File is too large. Maximum allowed size is 1 MB."
        }

    content = file_path.read_text(encoding="utf-8", errors="ignore")

    return {
        "status": "done",
        "file": str(file_path),
        "content": content[:5000]
    }

@app.get("/")
def root():
    return {
        "name": APP_NAME,
        "status": "running",
        "ui": "http://127.0.0.1:8000/ui",
        "default_model": LIGHT_MODEL,
        "heavy_model": HEAVY_MODEL
    }

@app.get("/health")
def health():
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])

        return {
            "jarvis": "running",
            "ollama": "running",
            "installed_models": [m.get("name") for m in models],
            "default_model": LIGHT_MODEL,
            "heavy_model": HEAVY_MODEL,
            "system": get_system_status()
        }

    except Exception as e:
        return {
            "jarvis": "running",
            "ollama": "not reachable",
            "error": str(e),
            "system": get_system_status()
        }

@app.post("/chat")
def chat(req: ChatRequest):
    reply, model = ask_ollama(req.message, req.mode)
    return {
        "model": model,
        "reply": reply
    }

@app.post("/command")
def command(req: CommandRequest):
    return run_agent_command(req)

@app.post("/agent")
def agent(req: CommandRequest):
    return run_agent_command(req)

def run_agent_command(req: CommandRequest):
    text = req.command.strip()
    cmd = text.lower()
    if "calculator" in cmd and (
        "calculate" in cmd or
        "execute" in cmd
    ):
        import re

        match = re.search(
            r"(?:calculate|execute)\s+(.+)$",
            text,
            re.IGNORECASE
        )

        if not match:
            return {
                "status": "error",
                "message": "Please include a calculation after the word calculate or execute."
            }

        expression = match.group(1).strip()

        try:
            result = open_calculator_and_calculate(
                expression
            )

            return {
                "status": "done",
                "message": "Calculator opened and calculation executed.",
                "expression": expression,
                "result": result
            }

        except ZeroDivisionError:
            return {
                "status": "error",
                "message": "Division by zero is not allowed."
            }

        except Exception as error:
            return {
                "status": "error",
                "message": str(error)
            }
    if cmd.startswith("read webpage "):
        url = text[len("read webpage "):].strip()

        try:
            return read_webpage(url)

        except Exception as error:
            return {
                "status": "error",
                "message": str(error)
            }

    if cmd.startswith("website screenshot "):
        url = text[len("website screenshot "):].strip()

        try:
            return screenshot_webpage(url)

        except Exception as error:
            return {
                "status": "error",
                "message": str(error)
            }

    if cmd.startswith("take website screenshot "):
        url = text[len("take website screenshot "):].strip()

        try:
            return screenshot_webpage(url)

        except Exception as error:
            return {
                "status": "error",
                "message": str(error)
            }
    dangerous_words = [
        "delete", "remove", "send email", "payment", "pay",
        "submit", "book slot", "server change", "production",
        "format", "shutdown", "restart", "erase"
    ]

    if any(word in cmd for word in dangerous_words):
        action_id = str(uuid.uuid4())
        pending_actions[action_id] = {
            "command": req.command,
            "created_at": time.time()
        }

        return {
            "status": "confirmation_required",
            "action_id": action_id,
            "message": "This action needs your confirmation before Jarvis can continue."
        }

    if cmd in ["system status", "status", "pc status", "laptop status"]:
        return {
            "status": "done",
            "message": "System status fetched.",
            "system": get_system_status()
        }

    if cmd in ["take screenshot", "screenshot", "capture screen"]:
        file_path = take_screenshot()
        return {
            "status": "done",
            "message": "Screenshot captured.",
            "file": file_path
        }

    if cmd.startswith("open app "):
        app_name = text.replace("open app", "", 1).strip()
        success, message = open_known_app(app_name)
        return {
            "status": "done" if success else "error",
            "message": message
        }

    if cmd.startswith("open folder "):
        folder_name = text.replace("open folder", "", 1).strip()
        success, message = open_known_folder(folder_name)
        return {
            "status": "done" if success else "error",
            "message": message
        }

    if cmd in ["open notepad", "start notepad"]:
        subprocess.Popen("notepad.exe")
        return {"status": "done", "message": "Opened Notepad."}

    if cmd in ["open calculator", "open calc", "start calculator"]:
        subprocess.Popen("calc.exe")
        return {"status": "done", "message": "Opened Calculator."}

    if cmd in ["open downloads", "open downloads folder"]:
        success, message = open_known_folder("downloads")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open desktop", "open desktop folder"]:
        success, message = open_known_folder("desktop")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open documents", "open documents folder"]:
        success, message = open_known_folder("documents")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open jarvis", "open jarvis folder"]:
        success, message = open_known_folder("jarvis")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open notes", "open notes folder"]:
        success, message = open_known_folder("notes")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open screenshots", "open screenshots folder"]:
        success, message = open_known_folder("screenshots")
        return {"status": "done" if success else "error", "message": message}

    if cmd.startswith("open website "):
        url = text.replace("open website", "", 1).strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        return {"status": "done", "message": f"Opened website: {url}"}

    if cmd.startswith("open "):
        target = text.replace("open", "", 1).strip()
        if "." in target and " " not in target:
            url = target
            if not url.startswith("http"):
                url = "https://" + url
            webbrowser.open(url)
            return {"status": "done", "message": f"Opened website: {url}"}

    if cmd.startswith("search google for "):
        query = text.replace("search google for", "", 1).strip()
        url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        webbrowser.open(url)
        return {"status": "done", "message": f"Searched Google for: {query}"}

    if cmd.startswith("google "):
        query = text.replace("google", "", 1).strip()
        url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        webbrowser.open(url)
        return {"status": "done", "message": f"Searched Google for: {query}"}

    if cmd.startswith("create note "):
        note_text = text.replace("create note", "", 1).strip()

        filename = datetime.datetime.now().strftime("note_%Y%m%d_%H%M%S.txt")
        file_path = NOTES_DIR / filename

        file_path.write_text(note_text, encoding="utf-8")

        return {
            "status": "done",
            "message": "Note created.",
            "file": str(file_path)
        }

    if cmd in ["list notes", "show notes"]:
        notes = sorted(NOTES_DIR.glob("*.txt"), reverse=True)
        return {
            "status": "done",
            "message": "Notes listed.",
            "notes": [str(note) for note in notes[:20]]
        }

    if cmd.startswith("read file "):
        file_path_text = text.replace("read file", "", 1).strip()
        return read_text_file(file_path_text)

    if cmd in ["list models", "ollama list"]:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True
        )

        return {
            "status": "done",
            "message": result.stdout
        }

    reply, model = ask_ollama(
        "User said: " + req.command + "\nReply as Jarvis. Keep it short and useful.",
        req.mode
    )

    return {
        "status": "ai_response",
        "model": model,
        "reply": reply
    }

@app.post("/confirm-action")
def confirm_action(req: ConfirmRequest):
    action = pending_actions.get(req.action_id)

    if not action:
        return {
            "status": "not_found",
            "message": "No pending action found."
        }

    if not req.confirm:
        pending_actions.pop(req.action_id, None)
        return {
            "status": "cancelled",
            "message": "Action cancelled."
        }

    original_command = action["command"]
    pending_actions.pop(req.action_id, None)

    return {
        "status": "confirmed",
        "message": "Confirmation received. Risky execution is disabled in this safe version.",
        "command": original_command
    }

@app.get("/ui")
def jarvis_ui():
    html = """
<!DOCTYPE html>
<html>
<head>
  <title>Jarvis Local Agent</title>
  <style>
    body {
      font-family: Arial;
      background: #0f172a;
      color: white;
      padding: 30px;
    }
    .box {
      max-width: 1000px;
      margin: auto;
      background: #111827;
      padding: 25px;
      border-radius: 15px;
    }
    h1 {
      color: #38bdf8;
    }
    textarea {
      width: 100%;
      height: 130px;
      font-size: 18px;
      padding: 15px;
      border-radius: 10px;
      background: #020617;
      color: white;
      border: 1px solid #334155;
      box-sizing: border-box;
    }
    button, select {
      margin-top: 12px;
      padding: 12px 18px;
      font-size: 16px;
      border-radius: 8px;
      border: none;
    }
    button {
      background: #2563eb;
      color: white;
      cursor: pointer;
    }
    .quick button {
      background: #334155;
      margin-right: 8px;
      margin-bottom: 8px;
    }
    pre {
      background: #020617;
      color: #22c55e;
      padding: 15px;
      border-radius: 10px;
      min-height: 180px;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <div class="box">
    <h1>🤖 Jarvis Local Agent - Phase 2</h1>
    <p>Type your command below and click Execute.</p>

    <textarea id="cmd" placeholder="Example: system status"></textarea>

    <br>

    <select id="mode">
      <option value="light">Light Mode - Fast</option>
      <option value="heavy">Heavy Mode - Deep Thinking</option>
    </select>

    <button onclick="runCommand()">Execute</button>
    <button onclick="clearAll()">Clear</button>

    <div class="quick">
      <h3>Quick Commands</h3>
      <button onclick="setCmd('system status')">System Status</button>
      <button onclick="setCmd('take screenshot')">Take Screenshot</button>
      <button onclick="setCmd('open notepad')">Open Notepad</button>
      <button onclick="setCmd('open calculator')">Open Calculator</button>
      <button onclick="setCmd('open downloads')">Open Downloads</button>
      <button onclick="setCmd('open app paint')">Open Paint</button>
      <button onclick="setCmd('open website youtube.com')">Open YouTube</button>
      <button onclick="setCmd('google latest AI tools')">Google Search</button>
      <button onclick="setCmd('create note Continue Jarvis setup')">Create Note</button>
      <button onclick="setCmd('list notes')">List Notes</button>
      <button onclick="setCmd('list models')">List Models</button>
      <button onclick="setCmd('Who are you?')">Ask Jarvis</button>
    </div>

    <h3>Response</h3>
    <pre id="output">Waiting for command...</pre>
  </div>

<script>
function setCmd(text) {
  document.getElementById("cmd").value = text;
}

function clearAll() {
  document.getElementById("cmd").value = "";
  document.getElementById("output").textContent = "Waiting for command...";
}

async function runCommand() {
  const command = document.getElementById("cmd").value;
  const mode = document.getElementById("mode").value;
  const output = document.getElementById("output");

  if (!command.trim()) {
    output.textContent = "Please enter a command.";
    return;
  }

  output.textContent = "Jarvis working...";

  try {
    const res = await fetch("/agent", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        command: command,
        mode: mode
      })
    });

    const data = await res.json();
    output.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    output.textContent = "Error: " + err;
  }
}
</script>
</body>
</html>
"""
    return HTMLResponse(html)