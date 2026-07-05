// Popup UI controller for the Browser Activity Logger extension.
//
// Responsibilities:
//   - Reflect current settings/state on open (logging + input-content toggles,
//     event count, session id).
//   - Persist toggle changes via setSettings.
//   - Export the current bundle as a downloadable JSON file (no downloads perm).
//   - Clear stored events after a confirm() guard.
import { getSettings, setSettings } from "./storage";
import type { ExportBundle } from "./types";

// ---- Message response shapes ---------------------------------------------
// The background service worker replies with these shapes. `ok: false` (or no
// response at all) is treated as a failure and surfaced to the user.

interface StatsResponse {
  ok: true;
  count: number;
  sessionId: string;
  loggingEnabled: boolean;
  saveInputContent: boolean;
  loggingStartedEpoch: number | null;
}

interface ClearResponse {
  ok: true;
}

interface ExportResponse {
  ok: true;
  bundle: ExportBundle;
}

// ---- DOM lookups ----------------------------------------------------------

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`[popup] missing element #${id}`);
  return node as T;
}

const masterBadge = el<HTMLSpanElement>("master-badge");
const toggleLogging = el<HTMLInputElement>("toggle-logging");
const toggleInput = el<HTMLInputElement>("toggle-input");
const eventCount = el<HTMLSpanElement>("event-count");
const uptimeEl = el<HTMLSpanElement>("logging-uptime");
const sessionId = el<HTMLElement>("session-id");
const btnExport = el<HTMLButtonElement>("btn-export");
const btnClear = el<HTMLButtonElement>("btn-clear");
const statusEl = el<HTMLDivElement>("status");

// ---- Messaging helper -----------------------------------------------------
// chrome.runtime.sendMessage resolves to `undefined` when no listener responds;
// we normalise that (and thrown errors) to `null` so callers can degrade
// gracefully instead of crashing the popup.
async function send<T>(message: unknown): Promise<T | null> {
  try {
    const res = (await chrome.runtime.sendMessage(message)) as T | undefined;
    return res ?? null;
  } catch (err) {
    console.error("[popup] sendMessage failed", err);
    return null;
  }
}

// ---- Status / toast -------------------------------------------------------

let statusTimer: number | undefined;

function setStatus(text: string, kind: "ok" | "error" | "muted" = "muted"): void {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind === "muted" ? "" : ` status--${kind}`);
  if (statusTimer !== undefined) window.clearTimeout(statusTimer);
  if (text) {
    statusTimer = window.setTimeout(() => {
      statusEl.textContent = "";
      statusEl.className = "status";
    }, 3000);
  }
}

// ---- Rendering ------------------------------------------------------------

function renderMasterBadge(enabled: boolean): void {
  masterBadge.textContent = enabled ? "ON" : "OFF";
  masterBadge.className = "badge " + (enabled ? "badge--on" : "badge--off");
}

function renderCount(count: number | null): void {
  eventCount.textContent = count === null ? "—" : String(count);
}

function renderSession(id: string | null): void {
  sessionId.textContent = id && id.length > 0 ? id : "—";
}

// ---- Logging uptime -------------------------------------------------------
// Epoch when logging was last turned ON (null while OFF). Re-rendered every
// second by a ticking timer so the popup shows a live "Logging for" duration.
let loggingStartedEpoch: number | null = null;

function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const s = totalSec % 60;
  const m = Math.floor(totalSec / 60) % 60;
  const h = Math.floor(totalSec / 3600);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function renderUptime(): void {
  uptimeEl.textContent =
    loggingStartedEpoch == null
      ? "—"
      : formatDuration(Date.now() - loggingStartedEpoch);
}

window.setInterval(renderUptime, 1000);

// ---- State loading --------------------------------------------------------
// Prefer the authoritative GET_STATS snapshot from the background worker; fall
// back to reading settings directly from storage if it does not respond.
async function refreshState(): Promise<void> {
  const stats = await send<StatsResponse>({ kind: "GET_STATS" });

  if (stats && stats.ok) {
    toggleLogging.checked = stats.loggingEnabled;
    toggleInput.checked = stats.saveInputContent;
    renderMasterBadge(stats.loggingEnabled);
    renderCount(stats.count);
    renderSession(stats.sessionId);
    loggingStartedEpoch = stats.loggingStartedEpoch;
    renderUptime();
    return;
  }

  // Background unavailable: still reflect stored settings so toggles are correct.
  const settings = await getSettings();
  toggleLogging.checked = settings.loggingEnabled;
  toggleInput.checked = settings.saveInputContent;
  renderMasterBadge(settings.loggingEnabled);
  renderCount(null);
  renderSession(null);
  loggingStartedEpoch = null;
  renderUptime();
  setStatus("Background not responding", "error");
}

// Lighter refresh used after clearing: just re-read the count/session.
async function refreshStats(): Promise<void> {
  const stats = await send<StatsResponse>({ kind: "GET_STATS" });
  if (stats && stats.ok) {
    renderCount(stats.count);
    renderSession(stats.sessionId);
  } else {
    renderCount(null);
  }
}

// ---- Toggle handlers ------------------------------------------------------

async function onToggleLogging(): Promise<void> {
  const enabled = toggleLogging.checked;
  renderMasterBadge(enabled);
  try {
    // Auto-save the collected log the moment logging is switched OFF, so a
    // session's data is never lost by forgetting to export manually.
    if (!enabled) {
      const saved = await downloadExport();
      if (saved && saved > 0) {
        setStatus(`Logging disabled — saved ${saved} event${saved === 1 ? "" : "s"}`, "ok");
      } else {
        setStatus("Logging disabled", "ok");
      }
    }

    await setSettings({ loggingEnabled: enabled });

    // Keep the local uptime clock in step with the toggle immediately; the
    // background worker is the authority but this avoids a visible lag.
    loggingStartedEpoch = enabled ? Date.now() : null;
    renderUptime();

    if (enabled) setStatus("Logging enabled", "ok");
  } catch (err) {
    console.error("[popup] failed to save loggingEnabled", err);
    toggleLogging.checked = !enabled;
    renderMasterBadge(!enabled);
    setStatus("Could not save setting", "error");
  }
}

async function onToggleInput(): Promise<void> {
  const enabled = toggleInput.checked;
  try {
    await setSettings({ saveInputContent: enabled });
    setStatus(enabled ? "Input content saving on" : "Input content saving off", "ok");
  } catch (err) {
    console.error("[popup] failed to save saveInputContent", err);
    toggleInput.checked = !enabled;
    setStatus("Could not save setting", "error");
  }
}

// ---- Export ---------------------------------------------------------------

function timestampForFilename(date: Date): string {
  const p = (n: number): string => String(n).padStart(2, "0");
  const y = date.getFullYear();
  const mo = p(date.getMonth() + 1);
  const d = p(date.getDate());
  const h = p(date.getHours());
  const mi = p(date.getMinutes());
  const s = p(date.getSeconds());
  return `${y}${mo}${d}-${h}${mi}${s}`;
}

// Fetch the current bundle and trigger a JSON file download. Returns the number
// of events written, or null on failure. Shared by the manual Export button and
// the automatic save-on-disable path.
async function downloadExport(): Promise<number | null> {
  const res = await send<ExportResponse>({ kind: "GET_EXPORT" });
  if (!res || !res.ok) return null;

  const json = JSON.stringify(res.bundle, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `browser-activity-${timestampForFilename(new Date())}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);

  return res.bundle.events.length;
}

async function onExport(): Promise<void> {
  btnExport.disabled = true;
  setStatus("Preparing export…");
  try {
    const n = await downloadExport();
    if (n === null) {
      setStatus("Export failed", "error");
      return;
    }
    setStatus(`Exported ${n} event${n === 1 ? "" : "s"}`, "ok");
  } catch (err) {
    console.error("[popup] export failed", err);
    setStatus("Export failed", "error");
  } finally {
    btnExport.disabled = false;
  }
}

// ---- Clear ----------------------------------------------------------------

async function onClear(): Promise<void> {
  // confirm() is safe here: an extension popup is not a web page context.
  if (!window.confirm("Delete all stored activity events? This cannot be undone.")) {
    return;
  }

  btnClear.disabled = true;
  setStatus("Clearing…");
  try {
    const res = await send<ClearResponse>({ kind: "CLEAR_EVENTS" });
    if (res && res.ok) {
      await refreshStats();
      setStatus("Logs cleared", "ok");
    } else {
      setStatus("Clear failed", "error");
    }
  } catch (err) {
    console.error("[popup] clear failed", err);
    setStatus("Clear failed", "error");
  } finally {
    btnClear.disabled = false;
  }
}

// ---- Session id copy ------------------------------------------------------

async function onCopySession(): Promise<void> {
  const id = sessionId.textContent;
  if (!id || id === "—") return;
  try {
    await navigator.clipboard.writeText(id);
    setStatus("Session ID copied", "ok");
  } catch {
    // Clipboard may be unavailable; the text is user-selectable as a fallback.
    setStatus("Copy unavailable — select manually", "error");
  }
}

// ---- Wire up --------------------------------------------------------------

toggleLogging.addEventListener("change", onToggleLogging);
toggleInput.addEventListener("change", onToggleInput);
btnExport.addEventListener("click", onExport);
btnClear.addEventListener("click", onClear);
sessionId.addEventListener("click", onCopySession);

void refreshState();
