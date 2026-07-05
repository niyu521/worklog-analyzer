import type { Settings } from "./types";

// chrome.storage.local keys.
export const STORAGE_KEYS = {
  settings: "bal_settings",
  events: "bal_events",
  session: "bal_session",
  // Epoch (ms) at which logging was last turned ON. Absent while logging is OFF.
  loggingStarted: "bal_logging_started",
} as const;

// Business SaaS domains where input *content* may be saved by default.
// Matched as suffix (see domainMatches in util). Users can edit these in Options.
export const DEFAULT_INPUT_ALLOWLIST: string[] = [
  "docs.google.com",
  "drive.google.com",
  "mail.google.com",
  "calendar.google.com",
  "github.com",
  "notion.so",
  "www.notion.so",
  "slack.com",
  "freee.co.jp",
];

// Domains excluded from all logging by default. Empty — user adds as needed.
export const DEFAULT_BLOCKLIST: string[] = [];

export const DEFAULT_MAX_EVENTS = 20000;

export const DEFAULT_SETTINGS: Settings = {
  loggingEnabled: true,
  saveInputContent: true,
  clickLogging: true,
  inputLogging: true,
  searchQueryLogging: true,
  formSubmitLogging: true,
  inputAllowlist: DEFAULT_INPUT_ALLOWLIST,
  blocklist: DEFAULT_BLOCKLIST,
  maxEvents: DEFAULT_MAX_EVENTS,
};

// Search engines and the URL params that carry the query.
export const SEARCH_ENGINES: { host: string; param: string; name: string }[] = [
  { host: "google.", param: "q", name: "Google" },
  { host: "bing.com", param: "q", name: "Bing" },
  { host: "search.yahoo.co.jp", param: "p", name: "Yahoo Japan" },
  { host: "search.yahoo.com", param: "p", name: "Yahoo" },
  { host: "duckduckgo.com", param: "q", name: "DuckDuckGo" },
  { host: "baidu.com", param: "wd", name: "Baidu" },
  { host: "ecosia.org", param: "q", name: "Ecosia" },
  { host: "startpage.com", param: "query", name: "Startpage" },
  { host: "brave.com", param: "q", name: "Brave" },
];

// Max lengths applied to captured strings, to keep the log compact and to
// avoid accidentally hoovering up huge blobs of page content.
export const LIMITS = {
  text: 200,
  value: 2000,
  nearbyText: 300,
  selector: 512,
} as const;

// URL schemes we never log (internal/privileged pages carry no useful signal).
export const IGNORED_URL_PREFIXES = [
  "chrome://",
  "chrome-extension://",
  "about:",
  "edge://",
  "brave://",
  "devtools://",
  "view-source:",
  "chrome-search://",
];
