// Shared type definitions for the Browser Activity Logger extension.
// The JSON schema version is bumped whenever the exported event shape changes,
// so downstream AI-analysis tooling can migrate old exports.
export const SCHEMA_VERSION = "1.1.0";

export type EventType =
  | "page_view"
  | "tab_activated"
  | "tab_updated"
  | "search_query"
  | "click"
  | "input"
  | "change" // select / checkbox / radio value committed
  | "key_down" // a replay-relevant key (Enter, Tab, arrows, shortcuts…)
  | "scroll" // throttled scroll position, for visual replay
  | "form_submit";

// Descriptor for a clicked or interacted DOM element. Everything here is
// best-effort metadata that helps an AI understand *what* was interacted with.
export interface ElementInfo {
  tag?: string;
  text?: string;
  ariaLabel?: string;
  title?: string;
  role?: string;
  name?: string;
  type?: string; // input type, e.g. "text", "search" (never "password" — those are dropped)
  href?: string;
  id?: string;
  className?: string;
  placeholder?: string;
  label?: string; // associated <label> text
  selector?: string; // CSS selector path
  xpath?: string;
  nearbyText?: string; // surrounding/contextual text
  x?: number; // click viewport coordinate
  y?: number;
  checked?: boolean; // for checkbox / radio targets
  value?: string; // element value attribute (non-sensitive; e.g. radio/checkbox value)
}

// A single field captured as part of a form_submit event.
export interface FormFieldInfo {
  name?: string;
  id?: string;
  type?: string;
  label?: string;
  ariaLabel?: string;
  placeholder?: string;
  value?: string; // only present when the domain is allowlisted and field is non-sensitive
  valueSaved: boolean;
  sensitive: boolean;
}

// The canonical event record. Optional fields are populated per event type.
export interface ActivityEvent {
  id: string;
  timestamp: string; // ISO 8601
  epoch: number; // ms since epoch, for easy cross-log correlation
  type: EventType;
  sessionId: string;

  url: string;
  title: string;
  domain: string;

  searchQuery?: string;
  searchEngine?: string;

  click?: ElementInfo;
  input?: ElementInfo; // metadata about the input / change / key target
  inputValue?: string; // actual typed content — only when saved (see inputSaved)
  formFields?: FormFieldInfo[];

  key?: string; // for key_down events, e.g. "Enter", "Tab", "Meta+k"
  scroll?: { x: number; y: number }; // for scroll events, page scroll offset in px
  viewport?: { w: number; h: number }; // innerWidth/Height when the event fired

  context?: string; // free-form surrounding context (nearby text, page section)

  // Security bookkeeping. sensitiveFlag = the event *could* contain sensitive
  // data and was therefore redacted; inputSaved = whether the raw input value
  // was actually persisted.
  sensitiveFlag: boolean;
  inputSaved: boolean;
}

// A partial event emitted by content scripts. The background service worker
// authoritatively fills id / timestamp / epoch / sessionId and re-validates.
export type EventDraft = Omit<
  ActivityEvent,
  "id" | "timestamp" | "epoch" | "sessionId"
> & {
  // Content scripts may propose a client timestamp; background overrides.
  clientEpoch?: number;
};

export interface Settings {
  // Master switch: when false, nothing is recorded at all.
  loggingEnabled: boolean;
  // When false, input *values* are never persisted (metadata still may be).
  saveInputContent: boolean;

  // Per-event-type toggles.
  clickLogging: boolean;
  inputLogging: boolean;
  searchQueryLogging: boolean;
  formSubmitLogging: boolean;

  // Domains where input *content* may be saved (business SaaS only).
  inputAllowlist: string[];
  // Domains excluded from all logging.
  blocklist: string[];

  maxEvents: number;
}

export interface SessionInfo {
  sessionId: string;
  startedAt: string;
  startedEpoch: number;
}

// Shape of the exported JSON file.
export interface ExportBundle {
  exportedAt: string;
  schemaVersion: string;
  sessionId: string;
  settings: Settings;
  events: ActivityEvent[];
}

// Runtime message protocol (content script -> background).
export type RuntimeMessage =
  | { kind: "EVENT"; draft: EventDraft }
  | { kind: "GET_SETTINGS" }
  | { kind: "GET_STATS" }
  | { kind: "CLEAR_EVENTS" }
  | { kind: "GET_EXPORT" };
