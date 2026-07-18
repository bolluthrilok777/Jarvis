# Jarvis Local Agent — Roadmap

**Current phase:** Phase 5A-2 — Gmail Monitoring v1 (COMPLETED 2026-07-17)

The following phases are planned but NOT yet implemented. They are listed here for planning purposes only.

---

## ✅ Phase 4 — Local Voice Input (COMPLETED)

---

## ✅ Phase 4.5 — Universal Natural-Language Action Engine (COMPLETED)

---

## ✅ Phase 5A-1 — AI Command Center & Multitask Queue (COMPLETED)

Phase 5A-1 adds a structured task queue with risk-based execution, concurrency control, and a full task lifecycle API. Users can submit multiple commands at once, review protected actions before execution, and track task status through the UI.

**Scope:** Task queue, risk classification, concurrency control, duplicate prevention, safety validation.

---

## ✅ Phase 5A-2 — Gmail Monitoring v1 (COMPLETED 2026-07-17)

Phase 5A-2 adds a safe, local Gmail monitoring system that runs 24/7 alongside Jarvis. It monitors Gmail for job-related emails, downloads JD attachments, creates draft replies (never auto-sent by default), and logs all important emails to JSONL files.

**Key features:**
- Background thread monitoring with configurable poll interval (1/5/10/15/30 min)
- AI + deterministic classification for job-related emails
- JD attachment download and safe storage to `jobs_jd/YYYY-MM-DD/`
- Draft-only safety mode (no auto-sending by default)
- Deduplication via `data/gmail_processed.json`
- Full REST API with 10 endpoints
- Web UI panel with live status and event table
- 30+ automated tests with mocks

**Scope:** Gmail API (read, download, draft). No auto-send unless explicitly configured. No multi-account. No email threading.

---

## Phase 5 — Offline Voice Output

Add text-to-speech output using a local TTS engine (e.g. piper-tts). Jarvis speaks responses aloud without internet.

**Scope:** Output only. Builds on Phase 4 voice pipeline.

---

## Phase 6 — SQLite Memory

Add persistent conversation memory and user preferences using a local SQLite database. Jarvis remembers past sessions, notes context, and can recall previous interactions.

**Scope:** Local only. No cloud sync.

---

## Phase 7 — Improved Browser Actions

Extend Playwright integration to support form filling, clicking, and structured page extraction. Enable AI-driven multi-step browser workflows.

**Scope:** Localhost and public sites. Dangerous actions require confirmation.

---

## Phase 8 — Screen Understanding and Vision

Add screenshot analysis using a local vision model. Jarvis can describe what is on screen, identify UI elements, and assist with visual tasks.

**Scope:** Desktop screenshots. Requires a vision-capable local model.

---

## Phase 9 — Reminders and Scheduled Jobs

Add time-based task scheduling. Users can set reminders or recurring jobs that Jarvis executes automatically.

**Scope:** Local scheduler only. No external calendar sync in this phase.

---

## Phase 10 — Email and Calendar (Partially covered by Phase 5A-2)

Integrate with local or API-based email (IMAP/SMTP) and calendar (CalDAV or Microsoft Graph). Jarvis can read, draft, and send messages with explicit user confirmation.

**Note:** Gmail monitoring (read, classify, draft) was delivered in Phase 5A-2. Remaining for this phase: multi-account support, email sending with confirmation, calendar integration, and non-Gmail providers.

**Scope:** All email and calendar actions require confirmation before execution.

---

## Phase 11 — VPS and Slot-Booking Integration

Add secure remote server management and booking-system integration. Jarvis can execute pre-approved server commands and interact with booking APIs.

**Scope:** Requires explicit allowlisting of servers and booking endpoints. All actions require confirmation.

---

## Phase 12 — Windows Startup, Watchdog, Recovery, Security, and Private Remote Access

- Windows startup: auto-start Jarvis on login via Task Scheduler (no admin required)
- Watchdog: monitor and restart Jarvis if it crashes
- Recovery: automatic log rotation, disk-space alerts, and error recovery
- Security hardening: request authentication, rate limiting, and audit logging
- Private remote access: optional encrypted tunnel (e.g. Tailscale) for accessing Jarvis from other devices on the same account

**Scope:** Localhost security baseline. No public internet exposure.

---

## AI Command Center and Monitoring (COMPLETED 2026-07-16)

Delivered the black/white/gold command center, command reflection, validated interpreter and safety pipeline, concurrent task groups, verified Calculator and Notepad actions, controlled Google/YouTube workflows, application adapter registry, append-only notes, and server-side activity monitoring.

Monitoring continues while the server runs, but schedule persistence across restarts remains future work. YouTube research records only safely available metadata and never downloads videos or fabricates transcripts.