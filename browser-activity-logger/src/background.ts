import type {
  ActivityEvent,
  EventDraft,
  RuntimeMessage,
  Settings,
} from "./types";
import { getActiveSession, startSession } from "./session";
import {
  appendEvent,
  clearEvents,
  getEventCount,
  getEvents,
  getLoggingStartedEpoch,
  getSettings,
  setLoggingStartedEpoch,
  setSettings,
} from "./storage";
import { DEFAULT_SETTINGS, STORAGE_KEYS } from "./constants";
import { SCHEMA_VERSION } from "./types";
import {
  domainMatches,
  extractSearchQuery,
  isLoggableUrl,
  makeId,
  safeDomain,
  truncateText,
} from "./util";

// In-memory, best-effort activity clock used only for session idle-rollover.
let lastActivityEpoch: number | null = null;

// Remember the last URL seen per tab so we can distinguish a real navigation
// (tab_updated) from a same-page status change.
const lastUrlByTab = new Map<number, string>();

// ---- Lifecycle ------------------------------------------------------------

chrome.runtime.onInstalled.addListener(async () => {
  await ensureSettingsInitialized();
  await startSession();
  await resetLoggingClock();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureSettingsInitialized();
  // Fresh browser launch => fresh working session.
  await startSession();
  await resetLoggingClock();
});

async function ensureSettingsInitialized(): Promise<void> {
  const raw = await chrome.storage.local.get("bal_settings");
  if (!raw["bal_settings"]) {
    await setSettings(DEFAULT_SETTINGS);
  }
}

// On launch/install, restart the "logging for" clock from now if logging is on,
// so the elapsed time reflects this browser session rather than counting
// downtime while the browser was closed.
async function resetLoggingClock(): Promise<void> {
  const settings = await getSettings();
  await setLoggingStartedEpoch(settings.loggingEnabled ? Date.now() : null);
}

// Keep the logging-uptime clock in sync when the master toggle flips anywhere
// (popup/options). OFF clears it; a fresh ON stamps the current time.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  const change = changes[STORAGE_KEYS.settings];
  if (!change) return;
  const wasOn = (change.oldValue as Settings | undefined)?.loggingEnabled ?? false;
  const isOn = (change.newValue as Settings | undefined)?.loggingEnabled ?? false;
  if (isOn && !wasOn) {
    void setLoggingStartedEpoch(Date.now());
  } else if (!isOn && wasOn) {
    void setLoggingStartedEpoch(null);
  }
});

// ---- Core recording -------------------------------------------------------

// Turn a draft into a stored event, applying all settings-level gates.
// `incognito` events are dropped unconditionally.
async function record(
  draft: EventDraft,
  opts: { incognito?: boolean } = {}
): Promise<void> {
  if (opts.incognito) return; // never record incognito / secret-mode activity

  const settings = await getSettings();
  if (!settings.loggingEnabled) return;

  if (!isLoggableUrl(draft.url)) return;
  if (draft.domain && domainMatches(draft.domain, settings.blocklist)) return;

  if (!isTypeEnabled(draft.type, settings)) return;

  // Global input-content kill switch: strip any value if disabled.
  if (!settings.saveInputContent) {
    if (draft.inputValue !== undefined) {
      draft.inputValue = undefined;
      draft.inputSaved = false;
    }
    if (draft.formFields) {
      draft.formFields = draft.formFields.map((f) => ({
        ...f,
        value: undefined,
        valueSaved: false,
      }));
    }
  }

  const now = Date.now();
  lastActivityEpoch = now;
  const session = await getActiveSession(lastActivityEpoch, now);

  const event: ActivityEvent = {
    ...draft,
    id: makeId("evt"),
    timestamp: new Date(now).toISOString(),
    epoch: now,
    sessionId: session.sessionId,
  };

  await appendEvent(event, settings.maxEvents);
}

function isTypeEnabled(type: EventDraft["type"], s: Settings): boolean {
  switch (type) {
    case "click":
      return s.clickLogging;
    case "input":
    case "change":
      return s.inputLogging;
    case "search_query":
      return s.searchQueryLogging;
    case "form_submit":
      return s.formSubmitLogging;
    // Navigation / key / scroll events are always on when logging is enabled.
    case "page_view":
    case "tab_activated":
    case "tab_updated":
    case "key_down":
    case "scroll":
      return true;
    default:
      return true;
  }
}

// Build a minimal draft skeleton from tab info.
function draftFromTab(
  tab: chrome.tabs.Tab,
  type: EventDraft["type"]
): EventDraft | null {
  const url = tab.url || tab.pendingUrl || "";
  if (!url) return null;
  return {
    type,
    url,
    title: truncateText(tab.title) || "",
    domain: safeDomain(url),
    sensitiveFlag: false,
    inputSaved: false,
  };
}

// ---- Tab / navigation listeners ------------------------------------------

chrome.tabs.onActivated.addListener(async (info) => {
  try {
    const tab = await chrome.tabs.get(info.tabId);
    const draft = draftFromTab(tab, "tab_activated");
    if (draft) await record(draft, { incognito: tab.incognito });
  } catch {
    /* tab may have closed */
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  const incognito = tab.incognito;

  // A URL change within the tab = navigation ("ページ遷移").
  if (changeInfo.url) {
    const prev = lastUrlByTab.get(tabId);
    lastUrlByTab.set(tabId, changeInfo.url);
    if (prev !== changeInfo.url) {
      const draft = draftFromTab({ ...tab, url: changeInfo.url }, "tab_updated");
      if (draft) await record(draft, { incognito });

      // Search-engine query extraction from the new URL.
      await maybeRecordSearch(changeInfo.url, tab, incognito);
    }
  }

  // Page finished loading = view ("ページ閲覧"). Title is reliable here.
  if (changeInfo.status === "complete" && tab.url) {
    lastUrlByTab.set(tabId, tab.url);
    const draft = draftFromTab(tab, "page_view");
    if (draft) await record(draft, { incognito });
    await maybeRecordSearch(tab.url, tab, incognito);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => lastUrlByTab.delete(tabId));

// Track the last search we recorded per tab to avoid duplicate search events
// firing from both the url-change and status-complete branches.
const lastSearchByTab = new Map<number, string>();

async function maybeRecordSearch(
  url: string,
  tab: chrome.tabs.Tab,
  incognito?: boolean
): Promise<void> {
  const match = extractSearchQuery(url);
  if (!match) return;
  const key = `${tab.id}|${match.engine}|${match.query}`;
  if (lastSearchByTab.get(tab.id ?? -1) === key) return;
  lastSearchByTab.set(tab.id ?? -1, key);

  await record(
    {
      type: "search_query",
      url,
      title: truncateText(tab.title) || "",
      domain: safeDomain(url),
      searchQuery: match.query,
      searchEngine: match.engine,
      sensitiveFlag: false,
      inputSaved: false,
    },
    { incognito }
  );
}

// ---- Messaging ------------------------------------------------------------

chrome.runtime.onMessage.addListener(
  (msg: RuntimeMessage, sender, sendResponse) => {
    handleMessage(msg, sender)
      .then((res) => sendResponse(res))
      .catch((err) => {
        console.error("[activity-logger] message error", err);
        sendResponse({ ok: false, error: String(err) });
      });
    return true; // keep the message channel open for the async response
  }
);

async function handleMessage(
  msg: RuntimeMessage,
  sender: chrome.runtime.MessageSender
): Promise<unknown> {
  switch (msg.kind) {
    case "EVENT": {
      await record(msg.draft, { incognito: sender.tab?.incognito });
      return { ok: true };
    }
    case "GET_SETTINGS":
      return { ok: true, settings: await getSettings() };
    case "GET_STATS": {
      const [count, settings, loggingStartedEpoch] = await Promise.all([
        getEventCount(),
        getSettings(),
        getLoggingStartedEpoch(),
      ]);
      const session = await getActiveSession(lastActivityEpoch, Date.now());
      return {
        ok: true,
        count,
        sessionId: session.sessionId,
        loggingEnabled: settings.loggingEnabled,
        saveInputContent: settings.saveInputContent,
        loggingStartedEpoch,
      };
    }
    case "CLEAR_EVENTS":
      await clearEvents();
      return { ok: true };
    case "GET_EXPORT": {
      const [events, settings] = await Promise.all([
        getEvents(),
        getSettings(),
      ]);
      const session = await getActiveSession(lastActivityEpoch, Date.now());
      return {
        ok: true,
        bundle: {
          exportedAt: new Date().toISOString(),
          schemaVersion: SCHEMA_VERSION,
          sessionId: session.sessionId,
          settings,
          events,
        },
      };
    }
    default:
      return { ok: false, error: "unknown message" };
  }
}
