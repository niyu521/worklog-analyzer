// Central place for all "never store this" logic. Kept small and auditable.
//
// Two layers of defense:
//   1. Field-level: if a field's type/name/id/placeholder/label/aria-label
//      looks like a credential, we never store its value.
//   2. Value-level: if a value itself pattern-matches a secret (card number,
//      long token, JWT, private key), we never store it — regardless of domain.

// Substrings that, when present in a field's metadata, mark it sensitive.
const SENSITIVE_FIELD_KEYWORDS = [
  "password",
  "passwd",
  "pwd",
  "token",
  "secret",
  "apikey",
  "api-key",
  "api_key",
  "authorization",
  "auth",
  "cookie",
  "credit",
  "card",
  "cardnumber",
  "cardno",
  "cvc",
  "cvv",
  "ccv",
  "otp",
  "2fa",
  "mfa",
  "totp",
  "pin",
  "ssn",
  "privatekey",
  "private-key",
  "private_key",
  "seed",
  "mnemonic",
  "passphrase",
  "securitycode",
  "security-code",
  "security_code",
];

// Autocomplete tokens that signal credential/payment fields.
const SENSITIVE_AUTOCOMPLETE = [
  "current-password",
  "new-password",
  "one-time-code",
  "cc-number",
  "cc-csc",
  "cc-exp",
];

function normalize(s: string | null | undefined): string {
  return (s || "").toLowerCase().replace(/[\s]/g, "");
}

// True if the field metadata indicates a credential/payment/secret field.
export function isSensitiveField(meta: {
  type?: string | null;
  name?: string | null;
  id?: string | null;
  placeholder?: string | null;
  label?: string | null;
  ariaLabel?: string | null;
  autocomplete?: string | null;
}): boolean {
  // Password inputs are always off-limits.
  if ((meta.type || "").toLowerCase() === "password") return true;

  const auto = normalize(meta.autocomplete);
  if (SENSITIVE_AUTOCOMPLETE.some((a) => auto.includes(a.replace(/-/g, ""))))
    return true;

  const haystack = [
    meta.name,
    meta.id,
    meta.placeholder,
    meta.label,
    meta.ariaLabel,
  ]
    .map(normalize)
    .join("|");

  return SENSITIVE_FIELD_KEYWORDS.some((kw) =>
    haystack.includes(kw.replace(/[-_]/g, ""))
  );
}

// Luhn check to reduce false positives on 13-19 digit numbers.
function passesLuhn(digits: string): boolean {
  let sum = 0;
  let alt = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = digits.charCodeAt(i) - 48;
    if (alt) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    alt = !alt;
  }
  return sum % 10 === 0;
}

// True if the *value itself* looks like a secret and must never be stored.
export function valueLooksSensitive(value: string): boolean {
  if (!value) return false;
  const v = value.trim();

  // Credit-card-like: 13-19 digits (allowing spaces/dashes) passing Luhn.
  const digitsOnly = v.replace(/[\s-]/g, "");
  if (/^\d{13,19}$/.test(digitsOnly) && passesLuhn(digitsOnly)) return true;

  // JWT: three base64url segments separated by dots.
  if (/^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$/.test(v))
    return true;

  // PEM private key block.
  if (/-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(v)) return true;

  // Common API key / token prefixes.
  if (/\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}/.test(v)) return true;
  if (/\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/.test(v))
    return true;
  if (/\bxox[baprs]-[A-Za-z0-9-]{10,}/.test(v)) return true; // Slack tokens
  if (/\bAKIA[0-9A-Z]{16}\b/.test(v)) return true; // AWS access key id
  if (/\bAIza[0-9A-Za-z_-]{20,}/.test(v)) return true; // Google API key
  if (/\bBearer\s+[A-Za-z0-9._-]{16,}/i.test(v)) return true;

  // Long high-entropy-looking hex/base64 blob (>=32 chars, no spaces).
  if (/^[A-Za-z0-9+/=_-]{40,}$/.test(v) && !/\s/.test(v)) return true;

  return false;
}
