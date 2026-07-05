import { getSettings, setSettings } from "./storage";
import { DEFAULT_SETTINGS } from "./constants";
import type { Settings } from "./types";

// ---- Element lookup -------------------------------------------------------

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`options: missing element #${id}`);
  }
  return node as T;
}

const form = el<HTMLFormElement>("settings-form");
const clickLogging = el<HTMLInputElement>("clickLogging");
const inputLogging = el<HTMLInputElement>("inputLogging");
const searchQueryLogging = el<HTMLInputElement>("searchQueryLogging");
const formSubmitLogging = el<HTMLInputElement>("formSubmitLogging");
const inputAllowlist = el<HTMLTextAreaElement>("inputAllowlist");
const blocklist = el<HTMLTextAreaElement>("blocklist");
const maxEvents = el<HTMLInputElement>("maxEvents");
const maxEventsError = el<HTMLParagraphElement>("maxEvents-error");
const resetBtn = el<HTMLButtonElement>("reset-btn");
const status = el<HTMLSpanElement>("status");

const MIN_MAX_EVENTS = 100;

// ---- Parsing / formatting helpers -----------------------------------------

// Textarea (one domain per line) -> string[] (trimmed, blanks removed, deduped).
function parseDomainList(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim().toLowerCase();
    if (line && !seen.has(line)) {
      seen.add(line);
      out.push(line);
    }
  }
  return out;
}

function formatDomainList(list: string[]): string {
  return list.join("\n");
}

// Returns a valid positive integer >= MIN_MAX_EVENTS, or null when invalid.
function parseMaxEvents(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "" || !/^\d+$/.test(trimmed)) {
    return null;
  }
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value <= 0) {
    return null;
  }
  return Math.max(MIN_MAX_EVENTS, value);
}

// ---- Status / error UI ----------------------------------------------------

let statusTimer: number | undefined;

function showStatus(message: string, kind: "ok" | "error"): void {
  status.textContent = message;
  status.classList.remove("status--ok", "status--error", "is-visible");
  status.classList.add(kind === "ok" ? "status--ok" : "status--error");
  // Force reflow so re-triggering the transition works on repeated saves.
  void status.offsetWidth;
  status.classList.add("is-visible");
  if (statusTimer !== undefined) {
    window.clearTimeout(statusTimer);
  }
  statusTimer = window.setTimeout(() => {
    status.classList.remove("is-visible");
  }, 2500);
}

function setMaxEventsError(message: string | null): void {
  if (message) {
    maxEventsError.textContent = message;
    maxEventsError.hidden = false;
    maxEvents.classList.add("is-invalid");
    maxEvents.setAttribute("aria-invalid", "true");
  } else {
    maxEventsError.textContent = "";
    maxEventsError.hidden = true;
    maxEvents.classList.remove("is-invalid");
    maxEvents.removeAttribute("aria-invalid");
  }
}

// ---- Populate / read the form ---------------------------------------------

function populate(settings: Settings): void {
  clickLogging.checked = settings.clickLogging;
  inputLogging.checked = settings.inputLogging;
  searchQueryLogging.checked = settings.searchQueryLogging;
  formSubmitLogging.checked = settings.formSubmitLogging;
  inputAllowlist.value = formatDomainList(settings.inputAllowlist);
  blocklist.value = formatDomainList(settings.blocklist);
  maxEvents.value = String(settings.maxEvents);
  setMaxEventsError(null);
}

// ---- Persistence ----------------------------------------------------------

async function save(): Promise<void> {
  const parsedMax = parseMaxEvents(maxEvents.value);
  if (parsedMax === null) {
    setMaxEventsError(
      `Enter a positive whole number (at least ${MIN_MAX_EVENTS}).`,
    );
    maxEvents.focus();
    showStatus("Could not save — please fix the highlighted field.", "error");
    return;
  }
  setMaxEventsError(null);

  const patch: Partial<Settings> = {
    clickLogging: clickLogging.checked,
    inputLogging: inputLogging.checked,
    searchQueryLogging: searchQueryLogging.checked,
    formSubmitLogging: formSubmitLogging.checked,
    inputAllowlist: parseDomainList(inputAllowlist.value),
    blocklist: parseDomainList(blocklist.value),
    maxEvents: parsedMax,
  };

  try {
    const saved = await setSettings(patch);
    // Re-populate so the UI reflects normalized values (clamped max, deduped
    // lists) exactly as they were persisted.
    populate(saved);
    showStatus("Saved ✓", "ok");
  } catch (err) {
    console.error("[activity-logger] failed to save settings", err);
    showStatus("Save failed — see console for details.", "error");
  }
}

async function resetToDefaults(): Promise<void> {
  try {
    const saved = await setSettings(DEFAULT_SETTINGS);
    populate(saved);
    showStatus("Restored defaults ✓", "ok");
  } catch (err) {
    console.error("[activity-logger] failed to reset settings", err);
    showStatus("Reset failed — see console for details.", "error");
  }
}

// ---- Wire up --------------------------------------------------------------

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void save();
});

resetBtn.addEventListener("click", () => {
  void resetToDefaults();
});

// Clear the inline error as soon as the user makes the field valid again.
maxEvents.addEventListener("input", () => {
  if (parseMaxEvents(maxEvents.value) !== null) {
    setMaxEventsError(null);
  }
});

async function init(): Promise<void> {
  try {
    populate(await getSettings());
  } catch (err) {
    console.error("[activity-logger] failed to load settings", err);
    populate(DEFAULT_SETTINGS);
    showStatus("Could not load saved settings — showing defaults.", "error");
  }
}

void init();
