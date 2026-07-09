# Jarvis Local Agent — Roadmap

**Current phase:** Phase 2 — Stabilized & Modularized

The following phases are planned but NOT yet implemented. They are listed here for planning purposes only.

---

## Phase 4 — Voice Input

Add microphone-based voice input using a local speech-to-text model (e.g. faster-whisper). Commands spoken aloud are transcribed and sent to the agent dispatcher.

**Scope:** Input only. No voice output in this phase.

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

## Phase 10 — Email and Calendar

Integrate with local or API-based email (IMAP/SMTP) and calendar (CalDAV or Microsoft Graph). Jarvis can read, draft, and send messages with explicit user confirmation.

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
