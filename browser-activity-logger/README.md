# Browser Activity Logger (Chrome MV3)

A Chrome extension that **automatically records your browser activity locally** and
lets you **export it as structured JSON** for later analysis by an AI. The goal is
the same as the work logs that Claude Code / Codex accumulate during a session —
but for the browser: which sites you visited, what you searched, which pages you
read, which UI elements you clicked, what you typed, and what business work you did.

> **Privacy by design.** Nothing is ever sent to a server. All data stays in
> `chrome.storage.local` on your device until *you* export or delete it. This tool
> is intended for use on **your own account** or on a **work device/account your
> employer has explicitly authorized**. It is not for stealth monitoring or
> covert collection.

## What gets recorded

While logging is ON, these events accumulate automatically (no "record" button):

| Event type | Trigger |
|---|---|
| `page_view` | A page finished loading |
| `tab_updated` | Navigation to a new URL within a tab |
| `tab_activated` | Switching to another tab |
| `search_query` | A search on Google / Bing / Yahoo / DuckDuckGo / etc. (query pulled from the URL) |
| `click` | Clicking a button, link, menu, field, or other element |
| `input` | Typing into a field (debounced; content saved only on allowlisted domains) |
| `form_submit` | Submitting a form |

Every event carries: `id`, `timestamp` (ISO) + `epoch` (ms), `type`, `sessionId`,
`url`, `title`, `domain`, plus type-specific detail (search query, click target
descriptor, input field descriptor, input value, surrounding context) and two
security flags: `sensitiveFlag` and `inputSaved`.

For clicks and inputs the element descriptor includes tag, text, `aria-label`,
`title`, `role`, `href`, `id`, `class`, name/type, associated `<label>`, a CSS
selector, an XPath, and nearby text — so an AI can tell *what* was interacted with.

## What is NEVER recorded (hard rules)

- `input type="password"` values — never even read.
- Any field whose name / id / placeholder / label / aria-label / autocomplete
  suggests a credential or payment field (password, token, secret, api_key,
  authorization, cookie, credit, card, cvc/cvv, otp, 2fa, pin, ssn, private key,
  seed/mnemonic, security code…).
- Any **value** that pattern-matches a secret — credit-card numbers (Luhn-checked),
  JWTs, PEM private keys, `sk_live_…`/`ghp_…`/`xox…`/`AKIA…`/`AIza…` tokens, bearer
  tokens, long high-entropy blobs — regardless of domain.
- Cookies, `Authorization` headers, `localStorage`/`sessionStorage` — the extension
  never touches these.
- **Incognito / secret-mode** activity is dropped unconditionally.

When content is withheld for a sensitive reason, the event still records the field
*metadata* with `sensitiveFlag: true` and `inputSaved: false`.

## Input-content allowlist

Input **content** (the actual typed text) is saved **only** on business-SaaS
domains in the allowlist. Everywhere else, only field metadata (type, label…) is
stored. Defaults: `docs.google.com`, `drive.google.com`, `mail.google.com`,
`calendar.google.com`, `github.com`, `notion.so`, `www.notion.so`, `slack.com`,
`freee.co.jp`. Edit this in **Options**. (Sensitive fields are still never saved,
even on allowlisted domains.)

## Popup

- Logging ON/OFF
- Input-content save ON/OFF
- Current stored event count
- Current session ID
- Export JSON
- Clear logs

## Options

- Input-content allowlist
- Blocklist (domains excluded from all logging)
- Max stored events (ring buffer; oldest dropped past the cap)
- Toggles: click / input / search-query / form-submit logging

## Sessions

Events are grouped by `sessionId`, started fresh on browser launch / install and
rolled over after a long idle gap. `sessionId` + timestamps + URL + domain +
event type are the join keys for correlating this log with Claude Code / Codex /
SaaS work logs later.

## Export format

```jsonc
{
  "exportedAt": "2026-07-05T…Z",
  "schemaVersion": "1.0.0",
  "sessionId": "sess_…",
  "settings": { /* current settings snapshot */ },
  "events": [ { "id": "evt_…", "timestamp": "…", "type": "click", /* … */ } ]
}
```

## Build & load

```bash
npm install
npm run build        # bundles src/ -> dist/ and copies manifest + html/css
# npm run watch      # rebuild on change
# npm run typecheck  # tsc --noEmit
```

Then in Chrome: `chrome://extensions` → enable **Developer mode** →
**Load unpacked** → select the `dist/` folder.

## Permissions (kept minimal)

- `storage` + `unlimitedStorage` — store the log locally.
- `tabs` — read URL/title on tab switch and navigation.
- Content script on `http/https` pages — capture clicks / inputs / submits.
- **No** host-server permissions, **no** `downloads`, **no** external network code.

## Architecture

- `src/background.ts` — service worker; the sole writer to storage. Emits tab /
  navigation / search events and receives interaction events from content scripts.
- `src/content.ts` — captures clicks, inputs (debounced), and form submits; applies
  the sensitive-data filters before anything leaves the page.
- `src/sensitive.ts` — all "never store this" logic, kept small and auditable.
- `src/storage.ts` — serialized read-modify-write append so concurrent events
  never clobber each other; ring-buffer trim.
- `src/session.ts` — session id lifecycle.
- `src/popup.ts` / `src/options.ts` — UI.
- `src/types.ts` — event schema + `SCHEMA_VERSION`.
