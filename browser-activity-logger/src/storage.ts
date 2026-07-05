import { DEFAULT_SETTINGS, STORAGE_KEYS } from "./constants";
import type { ActivityEvent, Settings } from "./types";

// ---- Settings -------------------------------------------------------------

export async function getSettings(): Promise<Settings> {
  const raw = await chrome.storage.local.get(STORAGE_KEYS.settings);
  const stored = raw[STORAGE_KEYS.settings] as Partial<Settings> | undefined;
  // Merge with defaults so newly added settings keys always have a value.
  return { ...DEFAULT_SETTINGS, ...(stored || {}) };
}

export async function setSettings(patch: Partial<Settings>): Promise<Settings> {
  const current = await getSettings();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ [STORAGE_KEYS.settings]: next });
  return next;
}

// ---- Logging uptime -------------------------------------------------------
// Tracks when logging was last switched ON so the popup can show elapsed time.

export async function getLoggingStartedEpoch(): Promise<number | null> {
  const raw = await chrome.storage.local.get(STORAGE_KEYS.loggingStarted);
  const v = raw[STORAGE_KEYS.loggingStarted];
  return typeof v === "number" ? v : null;
}

export async function setLoggingStartedEpoch(epoch: number | null): Promise<void> {
  if (epoch == null) {
    await chrome.storage.local.remove(STORAGE_KEYS.loggingStarted);
  } else {
    await chrome.storage.local.set({ [STORAGE_KEYS.loggingStarted]: epoch });
  }
}

// ---- Events ---------------------------------------------------------------

export async function getEvents(): Promise<ActivityEvent[]> {
  const raw = await chrome.storage.local.get(STORAGE_KEYS.events);
  return (raw[STORAGE_KEYS.events] as ActivityEvent[] | undefined) || [];
}

export async function clearEvents(): Promise<void> {
  await chrome.storage.local.set({ [STORAGE_KEYS.events]: [] });
}

export async function getEventCount(): Promise<number> {
  return (await getEvents()).length;
}

// chrome.storage is the single source of truth for events. Because both the
// background worker's own events and messages from many content scripts can
// arrive concurrently, we serialize read-modify-write through this promise
// chain so appends never clobber each other.
let writeChain: Promise<void> = Promise.resolve();

export function appendEvent(event: ActivityEvent, maxEvents: number): Promise<void> {
  writeChain = writeChain.then(async () => {
    const events = await getEvents();
    events.push(event);
    // Ring-buffer: drop oldest events beyond the cap.
    const trimmed =
      events.length > maxEvents ? events.slice(events.length - maxEvents) : events;
    await chrome.storage.local.set({ [STORAGE_KEYS.events]: trimmed });
  });
  // Swallow errors on the shared chain so one failure doesn't poison the rest.
  return writeChain.catch((err) => {
    console.error("[activity-logger] appendEvent failed", err);
  });
}
