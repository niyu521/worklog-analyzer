import { IGNORED_URL_PREFIXES, LIMITS, SEARCH_ENGINES } from "./constants";

// Small, dependency-free ID generator. Not cryptographic — just unique enough
// to correlate events. Uses crypto.randomUUID when available.
export function makeId(prefix = "evt"): string {
  const rnd =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  return `${prefix}_${rnd}`;
}

export function truncate(s: string | undefined | null, max: number): string | undefined {
  if (s == null) return undefined;
  const t = String(s).replace(/\s+/g, " ").trim();
  if (!t) return undefined;
  return t.length > max ? t.slice(0, max) + "…" : t;
}

export function truncateText(s: string | null | undefined): string | undefined {
  return truncate(s, LIMITS.text);
}

export function safeDomain(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

// Should this URL be logged at all (independent of user blocklist)?
export function isLoggableUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  return !IGNORED_URL_PREFIXES.some((p) => url.startsWith(p));
}

// Suffix-based domain match: "www.notion.so" matches allowlist entry "notion.so".
export function domainMatches(domain: string, list: string[]): boolean {
  const d = domain.toLowerCase();
  return list.some((raw) => {
    const entry = raw.trim().toLowerCase().replace(/^\*\.?/, "");
    if (!entry) return false;
    return d === entry || d.endsWith("." + entry);
  });
}

export interface SearchMatch {
  query: string;
  engine: string;
}

// Extract a search query from a URL if it's a known search engine SERP.
export function extractSearchQuery(url: string): SearchMatch | null {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  const host = u.hostname.toLowerCase();
  for (const eng of SEARCH_ENGINES) {
    if (host.includes(eng.host)) {
      const q = u.searchParams.get(eng.param);
      if (q && q.trim()) {
        return { query: truncate(q, LIMITS.value) || q, engine: eng.name };
      }
    }
  }
  return null;
}
