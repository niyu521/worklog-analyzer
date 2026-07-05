import { DEFAULT_SETTINGS, LIMITS } from "./constants";
import { isSensitiveField, valueLooksSensitive } from "./sensitive";
import type { ElementInfo, EventDraft, FormFieldInfo, Settings } from "./types";
import { safeDomain, truncate } from "./util";

// The content script observes DOM interaction and forwards structured drafts to
// the background worker, which is the sole writer to storage. All sensitive-data
// gating is applied here (first line of defense) and re-checked in background.

// ---- Settings cache -------------------------------------------------------
// Start from defaults so capture works immediately, before the async load
// resolves. (Previously this began as null and silently dropped every click /
// input until settings arrived — which could be never if the background worker
// was asleep, so nothing was recorded at all.)

let settings: Settings = DEFAULT_SETTINGS;

async function loadSettings(): Promise<Settings> {
  // Read straight from storage — no dependency on the background worker being
  // awake. Falls back to messaging only if storage is somehow unavailable.
  try {
    const raw = await chrome.storage.local.get("bal_settings");
    const stored = raw["bal_settings"] as Partial<Settings> | undefined;
    if (stored) settings = { ...DEFAULT_SETTINGS, ...stored };
    return settings;
  } catch {
    /* fall through to messaging */
  }
  try {
    const res = await chrome.runtime.sendMessage({ kind: "GET_SETTINGS" });
    if (res?.ok) settings = res.settings as Settings;
  } catch {
    /* keep defaults */
  }
  return settings;
}

// Keep the cache fresh when the user edits settings in Options/Popup.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["bal_settings"]) {
    settings = { ...DEFAULT_SETTINGS, ...(changes["bal_settings"].newValue as Settings) };
  }
});

const currentDomain = safeDomain(location.href);

// Input *content* is now captured on all domains (the user opted for full
// replay fidelity). Sensitive fields/values are still redacted downstream; the
// blocklist still excludes whole domains from any logging.
function inputContentAllowedHere(s: Settings): boolean {
  return s.loggingEnabled && s.saveInputContent && s.inputLogging;
}

// ---- Element description --------------------------------------------------

function cssSelector(el: Element): string {
  if (el.id) return `#${cssEscape(el.id)}`;
  const parts: string[] = [];
  let node: Element | null = el;
  let depth = 0;
  while (node && node.nodeType === 1 && depth < 5) {
    let part = node.nodeName.toLowerCase();
    if (node.id) {
      parts.unshift(`#${cssEscape(node.id)}`);
      break;
    }
    const parent = node.parentElement;
    if (parent) {
      const sameTag = Array.from(parent.children).filter(
        (c) => c.nodeName === node!.nodeName
      );
      if (sameTag.length > 1) {
        part += `:nth-of-type(${sameTag.indexOf(node) + 1})`;
      }
    }
    parts.unshift(part);
    node = node.parentElement;
    depth++;
  }
  return truncate(parts.join(" > "), LIMITS.selector) || part_fallback(el);
}

function part_fallback(el: Element): string {
  return el.nodeName.toLowerCase();
}

function cssEscape(s: string): string {
  if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(s);
  return s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function xpath(el: Element): string {
  const parts: string[] = [];
  let node: Element | null = el;
  let depth = 0;
  while (node && node.nodeType === 1 && depth < 8) {
    let index = 1;
    let sib = node.previousElementSibling;
    while (sib) {
      if (sib.nodeName === node.nodeName) index++;
      sib = sib.previousElementSibling;
    }
    parts.unshift(`${node.nodeName.toLowerCase()}[${index}]`);
    node = node.parentElement;
    depth++;
  }
  return truncate("/" + parts.join("/"), LIMITS.selector) || "";
}

function labelFor(el: Element): string | undefined {
  const id = el.getAttribute("id");
  if (id) {
    const lbl = document.querySelector(`label[for="${cssEscape(id)}"]`);
    if (lbl?.textContent) return truncate(lbl.textContent, LIMITS.text);
  }
  const wrap = el.closest("label");
  if (wrap?.textContent) return truncate(wrap.textContent, LIMITS.text);
  return undefined;
}

function nearbyText(el: Element): string | undefined {
  // Prefer a small, meaningful container over the whole page.
  const container =
    el.closest(
      "button, a, [role], li, td, th, section, article, header, nav, form, div"
    ) || el.parentElement;
  const text = container?.textContent || "";
  return truncate(text, LIMITS.nearbyText);
}

function describeElement(el: Element, includeCoords?: { x: number; y: number }): ElementInfo {
  const anyEl = el as HTMLElement & {
    type?: string;
    name?: string;
    placeholder?: string;
    value?: string;
    href?: string;
  };
  const info: ElementInfo = {
    tag: el.nodeName.toLowerCase(),
    text: truncate(el.textContent, LIMITS.text),
    ariaLabel: truncate(el.getAttribute("aria-label"), LIMITS.text),
    title: truncate(el.getAttribute("title"), LIMITS.text),
    role: el.getAttribute("role") || undefined,
    name: anyEl.name || el.getAttribute("name") || undefined,
    type: anyEl.type || el.getAttribute("type") || undefined,
    href: (el as HTMLAnchorElement).href || el.getAttribute("href") || undefined,
    id: el.id || undefined,
    className:
      typeof el.className === "string" && el.className
        ? truncate(el.className, LIMITS.text)
        : undefined,
    placeholder: truncate(anyEl.placeholder, LIMITS.text),
    label: labelFor(el),
    selector: cssSelector(el),
    xpath: xpath(el),
    nearbyText: nearbyText(el),
  };
  if (includeCoords) {
    info.x = Math.round(includeCoords.x);
    info.y = Math.round(includeCoords.y);
  }
  return info;
}

function fieldMeta(el: Element) {
  const anyEl = el as HTMLInputElement;
  return {
    type: anyEl.type || el.getAttribute("type"),
    name: anyEl.name || el.getAttribute("name"),
    id: el.id,
    placeholder: anyEl.placeholder || el.getAttribute("placeholder"),
    label: labelFor(el),
    ariaLabel: el.getAttribute("aria-label"),
    autocomplete: el.getAttribute("autocomplete"),
  };
}

// ---- Draft plumbing -------------------------------------------------------

function baseDraft(type: EventDraft["type"]): EventDraft {
  return {
    type,
    url: location.href,
    title: truncate(document.title, LIMITS.text) || "",
    domain: currentDomain,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    sensitiveFlag: false,
    inputSaved: false,
  };
}

function send(draft: EventDraft): void {
  try {
    chrome.runtime.sendMessage({ kind: "EVENT", draft }).catch(() => {});
  } catch {
    /* extension context may be invalidated on reload */
  }
}

// ---- Click capture --------------------------------------------------------

document.addEventListener(
  "click",
  (e) => {
    const s = settings;
    if (!s.loggingEnabled || !s.clickLogging) return;
    const target = e.target as Element | null;
    if (!target || target.nodeType !== 1) return;

    // Attribute the click to the nearest meaningful interactive ancestor.
    const el =
      target.closest(
        "a, button, [role='button'], [role='link'], [role='menuitem'], input, select, textarea, [onclick], label, summary"
      ) || target;

    const draft = baseDraft("click");
    draft.click = describeElement(el, { x: e.clientX, y: e.clientY });
    draft.context = draft.click.nearbyText;
    send(draft);
  },
  { capture: true, passive: true }
);

// ---- Input capture (debounced) -------------------------------------------

interface Pending {
  el: Element;
  timer: number;
}
const pending = new WeakMap<Element, Pending>();
const IDLE_MS = 1200;

function isEditable(el: Element): boolean {
  const tag = el.nodeName.toLowerCase();
  if (tag === "input" || tag === "textarea") return true;
  if ((el as HTMLElement).isContentEditable) return true;
  return false;
}

function flushInput(el: Element): void {
  const s = settings;
  if (!s.loggingEnabled || !s.inputLogging) return;
  if (!el.isConnected) return;

  const meta = fieldMeta(el);
  // Password fields are never even read.
  if ((meta.type || "").toLowerCase() === "password") return;

  const rawValue = readValue(el);
  const fieldSensitive = isSensitiveField(meta);
  const valueSensitive = rawValue ? valueLooksSensitive(rawValue) : false;
  const sensitive = fieldSensitive || valueSensitive;

  const draft = baseDraft("input");
  draft.input = describeElement(el);

  const allowedHere = inputContentAllowedHere(s);
  if (allowedHere && !sensitive && rawValue) {
    draft.inputValue = truncate(rawValue, LIMITS.value);
    draft.inputSaved = true;
    draft.sensitiveFlag = false;
  } else {
    // Metadata only. Flag it if the reason we withheld was sensitivity.
    draft.inputSaved = false;
    draft.sensitiveFlag = sensitive;
  }
  draft.context = draft.input.label || draft.input.nearbyText;
  send(draft);
}

function readValue(el: Element): string {
  const anyEl = el as HTMLInputElement;
  if (typeof anyEl.value === "string") return anyEl.value;
  if ((el as HTMLElement).isContentEditable) return el.textContent || "";
  return "";
}

function scheduleFlush(el: Element): void {
  const existing = pending.get(el);
  if (existing) clearTimeout(existing.timer);
  const timer = window.setTimeout(() => {
    pending.delete(el);
    flushInput(el);
  }, IDLE_MS);
  pending.set(el, { el, timer });
}

document.addEventListener(
  "input",
  (e) => {
    const s = settings;
    if (!s.loggingEnabled || !s.inputLogging) return;
    const el = e.target as Element | null;
    if (!el || !isEditable(el)) return;
    // Skip password fields entirely.
    if (((el as HTMLInputElement).type || "").toLowerCase() === "password")
      return;
    scheduleFlush(el);
  },
  { capture: true, passive: true }
);

// Flush immediately when a field loses focus so we don't lose the final value.
document.addEventListener(
  "focusout",
  (e) => {
    const el = e.target as Element | null;
    if (!el || el.nodeType !== 1) return;
    const p = pending.get(el);
    if (p) {
      clearTimeout(p.timer);
      pending.delete(el);
      flushInput(el);
    }
  },
  { capture: true, passive: true }
);

// ---- Form submit capture --------------------------------------------------

document.addEventListener(
  "submit",
  (e) => {
    const s = settings;
    if (!s.loggingEnabled || !s.formSubmitLogging) return;
    const form = e.target as HTMLFormElement | null;
    if (!form || form.nodeName.toLowerCase() !== "form") return;

    const allowedHere = inputContentAllowedHere(s);
    const fields: FormFieldInfo[] = [];
    let anySensitive = false;

    for (const el of Array.from(form.elements)) {
      const tag = el.nodeName.toLowerCase();
      if (tag !== "input" && tag !== "textarea" && tag !== "select") continue;
      const type = ((el as HTMLInputElement).type || "").toLowerCase();
      if (type === "hidden" || type === "submit" || type === "button") continue;

      const meta = fieldMeta(el);
      const rawValue = type === "password" ? "" : readValue(el);
      const fieldSensitive =
        type === "password" || isSensitiveField(meta) || valueLooksSensitive(rawValue);
      if (fieldSensitive) anySensitive = true;

      const canSave = allowedHere && !fieldSensitive && !!rawValue;
      fields.push({
        name: meta.name || undefined,
        id: meta.id || undefined,
        type: meta.type || undefined,
        label: meta.label || undefined,
        ariaLabel: truncate(meta.ariaLabel, LIMITS.text),
        placeholder: truncate(meta.placeholder, LIMITS.text),
        value: canSave ? truncate(rawValue, LIMITS.value) : undefined,
        valueSaved: canSave,
        sensitive: fieldSensitive,
      });
    }

    const draft = baseDraft("form_submit");
    draft.formFields = fields;
    draft.sensitiveFlag = anySensitive;
    draft.inputSaved = fields.some((f) => f.valueSaved);
    draft.context = truncate(
      form.getAttribute("aria-label") || form.getAttribute("name") || form.id,
      LIMITS.text
    );
    send(draft);
  },
  { capture: true, passive: true }
);

// ---- Change capture (select / checkbox / radio) ---------------------------
// `input` covers free-text as-you-type; `change` captures committed values for
// selects and the checked state of checkboxes/radios — needed to replay a form.

document.addEventListener(
  "change",
  (e) => {
    const s = settings;
    if (!s.loggingEnabled || !s.inputLogging) return;
    const el = e.target as Element | null;
    if (!el || el.nodeType !== 1) return;
    const tag = el.nodeName.toLowerCase();
    if (tag !== "input" && tag !== "select" && tag !== "textarea") return;

    const anyEl = el as HTMLInputElement;
    const type = (anyEl.type || "").toLowerCase();
    if (type === "password") return; // never read password fields

    const draft = baseDraft("change");
    draft.input = describeElement(el);

    if (type === "checkbox" || type === "radio") {
      // Checked state + the field's own value attribute are safe to record and
      // are exactly what a replay needs; no free-text is involved.
      draft.input.checked = anyEl.checked;
      draft.input.value = truncate(anyEl.value, LIMITS.value);
      draft.inputSaved = true;
    } else {
      const meta = fieldMeta(el);
      const raw = readValue(el);
      const sensitive = isSensitiveField(meta) || (raw ? valueLooksSensitive(raw) : false);
      if (inputContentAllowedHere(s) && !sensitive && raw) {
        draft.inputValue = truncate(raw, LIMITS.value);
        draft.inputSaved = true;
      } else {
        draft.sensitiveFlag = sensitive;
      }
      // For a <select>, also record the visible option label for readability.
      if (tag === "select") {
        const sel = el as unknown as HTMLSelectElement;
        const opt = sel.selectedOptions && sel.selectedOptions[0];
        if (opt) draft.context = truncate(opt.textContent, LIMITS.text);
      }
    }
    if (!draft.context) draft.context = draft.input.label || draft.input.nearbyText;
    send(draft);
  },
  { capture: true, passive: true }
);

// ---- Key capture (replay-relevant keys only) ------------------------------
// We deliberately do NOT log character keys (that would duplicate `input` and
// risk capturing secrets keystroke-by-keystroke). Only navigation/command keys
// and modifier shortcuts, which drive form submits, navigation, and hotkeys.

const REPLAY_KEYS = new Set([
  "Enter",
  "Tab",
  "Escape",
  "Backspace",
  "Delete",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "PageUp",
  "PageDown",
  "Home",
  "End",
]);

function keyDescriptor(e: KeyboardEvent): string {
  const mods: string[] = [];
  if (e.ctrlKey) mods.push("Ctrl");
  if (e.metaKey) mods.push("Meta");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  return [...mods, e.key].join("+");
}

document.addEventListener(
  "keydown",
  (e) => {
    const s = settings;
    if (!s.loggingEnabled) return;
    // Record a key when it's a navigation/command key, OR any key combined with
    // a Ctrl/Meta/Alt modifier (i.e. a shortcut like Cmd+K, Ctrl+S).
    const isShortcut = e.ctrlKey || e.metaKey || e.altKey;
    if (!REPLAY_KEYS.has(e.key) && !isShortcut) return;

    const draft = baseDraft("key_down");
    draft.key = keyDescriptor(e);
    const el = e.target as Element | null;
    if (el && el.nodeType === 1) {
      draft.input = describeElement(el);
      draft.context = draft.input.label || draft.input.nearbyText;
    }
    send(draft);
  },
  { capture: true, passive: true }
);

// ---- Scroll capture (throttled) -------------------------------------------
// Scroll position is part of faithfully reproducing a session, but is noisy, so
// it is throttled and only emitted after a meaningful positional change.

let lastScrollEmit = 0;
let lastScrollY = -1;
let lastScrollX = -1;

function onScroll(): void {
  const s = settings;
  if (!s.loggingEnabled) return;
  const now = Date.now();
  const x = Math.round(window.scrollX);
  const y = Math.round(window.scrollY);
  if (now - lastScrollEmit < 700) return;
  if (Math.abs(y - lastScrollY) < 40 && Math.abs(x - lastScrollX) < 40) return;
  lastScrollEmit = now;
  lastScrollY = y;
  lastScrollX = x;
  const draft = baseDraft("scroll");
  draft.scroll = { x, y };
  send(draft);
}

window.addEventListener("scroll", onScroll, { capture: true, passive: true });

// ---- Init -----------------------------------------------------------------

loadSettings();
