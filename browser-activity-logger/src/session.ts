import { STORAGE_KEYS } from "./constants";
import type { SessionInfo } from "./types";
import { makeId } from "./util";

// A session groups all events produced during one "working stretch" of the
// browser. We start a fresh session on browser startup / extension install,
// and also roll over after a long idle gap so distinct work sessions don't
// bleed together. sessionId is the join key for correlating with Claude Code /
// Codex work logs later.

const IDLE_ROLLOVER_MS = 6 * 60 * 60 * 1000; // 6h without activity => new session

export async function ensureSession(): Promise<SessionInfo> {
  const raw = await chrome.storage.local.get(STORAGE_KEYS.session);
  const existing = raw[STORAGE_KEYS.session] as SessionInfo | undefined;
  if (existing?.sessionId) return existing;
  return startSession();
}

export async function startSession(): Promise<SessionInfo> {
  const now = Date.now();
  const info: SessionInfo = {
    sessionId: makeId("sess"),
    startedAt: new Date(now).toISOString(),
    startedEpoch: now,
  };
  await chrome.storage.local.set({ [STORAGE_KEYS.session]: info });
  return info;
}

// Returns the active session, rolling over to a new one if idle too long.
// `lastActivityEpoch` is tracked in memory by the caller (background worker).
export async function getActiveSession(
  lastActivityEpoch: number | null,
  now: number
): Promise<SessionInfo> {
  if (lastActivityEpoch != null && now - lastActivityEpoch > IDLE_ROLLOVER_MS) {
    return startSession();
  }
  return ensureSession();
}
