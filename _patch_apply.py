import json
import sys

# ── Patch 1: gmail_monitor.py ─────────────────────────────────────────────
with open("jarvis_core/gmail_monitor.py", "r", encoding="utf-8") as f:
    src = f.read()

# Fix 1a: add snapshot before result dict in run_gmail_scan
old1 = '    result = {\n        "emails_scanned": 0,\n        "important_found": 0,\n        "jd_files_saved": 0,\n        "drafts_created": 0,\n        "errors": 0,\n        "warnings": [],\n    }\n\n    logger.info("Gmail scan started (source=%s)", source)'
new1 = '    # Snapshot state counters BEFORE scan to compute delta for THIS scan only.\n    with _lock:\n        _snap_important = _state.important_mails_today\n        _snap_jd = _state.jd_files_saved_today\n        _snap_drafts = _state.drafts_created_today + _state.local_drafts_today\n\n    result = {\n        "emails_scanned": 0,\n        "important_found": 0,\n        "jd_files_saved": 0,\n        "drafts_created": 0,\n        "errors": 0,\n        "warnings": [],\n    }\n\n    logger.info("Gmail scan started (source=%s)", source)'

if old1 not in src:
    print("PATCH 1a: text not found (may already be applied)")
else:
    src = src.replace(old1, new1, 1)
    print("PATCH 1a applied")

# Fix 1b: replace state-reading with delta at end of run_gmail_scan
old2 = '    _save_processed_ids(processed_ids)\n    # Read actual counts from state (set by _process_email during this scan)\n    with _lock:\n        result["important_found"] = _state.important_mails_today\n        result["jd_files_saved"] = _state.jd_files_saved_today\n        result["drafts_created"] = _state.drafts_created_today + _state.local_drafts_today\n    logger.info('
new2 = '    _save_processed_ids(processed_ids)\n    # Compute delta: counts for THIS scan only (not accumulated session total).\n    with _lock:\n        result["important_found"] = max(0, _state.important_mails_today - _snap_important)\n        result["jd_files_saved"] = max(0, _state.jd_files_saved_today - _snap_jd)\n        result["drafts_created"] = max(0, (_state.drafts_created_today + _state.local_drafts_today) - _snap_drafts)\n    logger.info('

if old2 not in src:
    print("PATCH 1b: text not found (may already be applied)")
else:
    src = src.replace(old2, new2, 1)
    print("PATCH 1b applied")

# Fix 1c: append clear_processed_ids
append_code = '''

def clear_processed_ids() -> dict:
    """Clear processed message IDs so all unread emails are re-scanned next cycle.

    Use this when user wants to force a full re-scan of existing emails.
    Never exposes tokens or credentials.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed_ids": []}, f)
        logger.info("Processed IDs cleared -- next scan will re-process all unread emails")
        return {"status": "ok", "message": "Processed email IDs cleared. Next scan will check all unread emails."}
    except Exception as e:
        logger.error("Failed to clear processed IDs: %s", e)
        return {"status": "error", "message": "Failed to clear processed IDs."}
'''

if "def clear_processed_ids" not in src:
    src = src.rstrip() + "\n" + append_code
    print("PATCH 1c applied: clear_processed_ids added")
else:
    print("PATCH 1c: clear_processed_ids already present")

with open("jarvis_core/gmail_monitor.py", "w", encoding="utf-8") as f:
    f.write(src)
print("gmail_monitor.py written OK")

# ── Patch 2: gmail_routes.py ──────────────────────────────────────────────
with open("jarvis_core/gmail_routes.py", "r", encoding="utf-8") as f:
    rsrc = f.read()

# Fix 2a: add clear_processed_ids to import block
old_imp = 'from jarvis_core.gmail_monitor import (\n    check_now,\n    get_config,\n    get_events_today,\n    get_state,\n    load_config,\n    start_monitor,\n    stop_monitor,\n    update_config,\n)'
new_imp = 'from jarvis_core.gmail_monitor import (\n    check_now,\n    clear_processed_ids,\n    get_config,\n    get_events_today,\n    get_state,\n    load_config,\n    start_monitor,\n    stop_monitor,\n    update_config,\n)'

if old_imp not in rsrc:
    print("PATCH 2a: import not found (may already be applied)")
else:
    rsrc = rsrc.replace(old_imp, new_imp, 1)
    print("PATCH 2a applied: clear_processed_ids imported")

# Fix 2b: add /clear-processed route before _count_events_for_period
route_code = '''@router.post("/clear-processed")
def gmail_clear_processed():
    """Clear processed email IDs so all unread emails are re-scanned.

    Use this to force a full re-scan when emails were already marked processed.
    Never exposes tokens or secrets.
    """
    result = clear_processed_ids()
    return result


'''

if '"/clear-processed"' not in rsrc:
    rsrc = rsrc.replace(
        "def _count_events_for_period(",
        route_code + "def _count_events_for_period(",
        1
    )
    print("PATCH 2b applied: /clear-processed route added")
else:
    print("PATCH 2b: /clear-processed already present")

with open("jarvis_core/gmail_routes.py", "w", encoding="utf-8") as f:
    f.write(rsrc)
print("gmail_routes.py written OK")

# ── Patch 3: ui.html ─────────────────────────────────────────────────────
with open("ui.html", "r", encoding="utf-8") as f:
    usrc = f.read()

# Fix 3a: add Re-scan All button after Check Now button
old_btn = '        <button class="btn btn-ghost" onclick="gmailCheckNow()" id="check-now-btn"><span id="check-now-text">\U0001f504 Check Now</span></button>'
new_btn = '        <button class="btn btn-ghost" onclick="gmailCheckNow()" id="check-now-btn"><span id="check-now-text">\U0001f504 Check Now</span></button>\n        <button class="btn btn-ghost" onclick="gmailReScanAll()" id="rescan-btn" title="Clear processed list and re-scan all unread emails" style="font-size:12px;padding:6px 10px;">\u21a9 Re-scan All</button>'

if old_btn in usrc:
    usrc = usrc.replace(old_btn, new_btn, 1)
    print("PATCH 3a applied: Re-scan All button added")
else:
    # Try with literal emoji
    old_btn2 = '        <button class="btn btn-ghost" onclick="gmailCheckNow()" id="check-now-btn"><span id="check-now-text">' + chr(0x1F504) + ' Check Now</span></button>'
    new_btn2 = old_btn2 + '\n        <button class="btn btn-ghost" onclick="gmailReScanAll()" id="rescan-btn" title="Clear processed list and re-scan all unread emails" style="font-size:12px;padding:6px 10px;">' + chr(0x21A9) + ' Re-scan All</button>'
    if old_btn2 in usrc:
        usrc = usrc.replace(old_btn2, new_btn2, 1)
        print("PATCH 3a (v2) applied: Re-scan All button added")
    else:
        print("PATCH 3a: check-now button not found, trying grep...")
        idx = usrc.find("check-now-btn")
        if idx >= 0:
            # Find the end of this button tag
            end = usrc.find("</button>", idx)
            if end >= 0:
                insert_pos = end + len("</button>")
                insert_text = '\n        <button class="btn btn-ghost" onclick="gmailReScanAll()" id="rescan-btn" title="Clear processed list and re-scan all unread emails" style="font-size:12px;padding:6px 10px;">' + chr(0x21A9) + ' Re-scan All</button>'
                usrc = usrc[:insert_pos] + insert_text + usrc[insert_pos:]
                print("PATCH 3a (v3) applied via index")
            else:
                print("PATCH 3a: could not find button end tag")
        else:
            print("PATCH 3a FAILED: check-now-btn not found at all")

# Fix 3b: add gmailReScanAll function
rescan_fn = """
async function gmailReScanAll() {
  var btn = document.getElementById('rescan-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing...'; }
  try {
    var r = await fetch('/gmail/clear-processed', {method: 'POST'});
    var d = await r.json();
    if (d.status === 'ok') {
      showGmailError('Processed list cleared. Running full re-scan...');
      if (btn) { btn.disabled = false; btn.innerHTML = '\\u21a9 Re-scan All'; }
      await gmailCheckNow();
    } else {
      showGmailError('Failed to clear: ' + (d.message || 'Unknown error'));
    }
  } catch(e) {
    showGmailError('Re-scan failed: network error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '\\u21a9 Re-scan All'; }
  }
}
"""

if "gmailReScanAll" not in usrc:
    # Insert before async function gmailRefresh
    marker = "async function gmailRefresh() {"
    if marker in usrc:
        usrc = usrc.replace(marker, rescan_fn + "\n" + marker, 1)
        print("PATCH 3b applied: gmailReScanAll function added")
    else:
        print("PATCH 3b FAILED: gmailRefresh not found")
else:
    print("PATCH 3b: gmailReScanAll already present")

with open("ui.html", "w", encoding="utf-8") as f:
    f.write(usrc)
print("ui.html written OK")
print("ALL PATCHES COMPLETE")
