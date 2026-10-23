/**
 * Typed client for the Gist API.
 *
 * Two things here are not incidental:
 *
 *  - Slow work is a job. `notes()` and `research()` return a job id, and
 *    `awaitJob` polls it. A phone cannot hold a four-minute request open —
 *    iOS suspends network activity the moment the app backgrounds — so the
 *    result has to be fetchable later by id rather than streamed back.
 *
 *  - The session cookie is stored by hand. React Native's fetch does not
 *    persist cookies reliably across app launches, so the Set-Cookie value is
 *    kept in the keychain and replayed on every request.
 */

import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";

const COOKIE_KEY = "gist.session";

export const API_BASE: string =
  (Constants.expoConfig?.extra as { apiBase?: string } | undefined)?.apiBase ??
  "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let cookie: string | null = null;

async function loadCookie(): Promise<string | null> {
  if (cookie === null) cookie = (await SecureStore.getItemAsync(COOKIE_KEY)) ?? "";
  return cookie || null;
}

async function saveCookie(header: string | null): Promise<void> {
  if (!header) return;
  // "yn_session=abc; Path=/; HttpOnly" -> "yn_session=abc"
  const pair = header.split(";")[0]?.trim();
  if (!pair) return;
  cookie = pair;
  await SecureStore.setItemAsync(COOKIE_KEY, pair);
}

export async function clearSession(): Promise<void> {
  cookie = "";
  await SecureStore.deleteItemAsync(COOKIE_KEY);
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const jar = await loadCookie();
  const res = await fetch(API_BASE + path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(jar ? { Cookie: jar } : {}),
      ...(init.headers ?? {}),
    },
  });

  // Some RN versions expose only the non-standard getter.
  const setCookie =
    res.headers.get("set-cookie") ??
    (res.headers as unknown as { map?: Record<string, string> }).map?.["set-cookie"] ??
    null;
  await saveCookie(setCookie);

  const text = await res.text();
  const body = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  if (!res.ok) {
    throw new ApiError(
      (body.error as string) ?? (body.detail as string) ?? `Request failed (${res.status})`,
      res.status,
    );
  }
  return body as T;
}

const get = <T,>(p: string) => request<T>(p);
const post = <T,>(p: string, body?: unknown) =>
  request<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const del = <T,>(p: string) => request<T>(p, { method: "DELETE" });

/* ── Types ─────────────────────────────────────────────────── */

export type User = { id: string; email: string; plan: string; is_admin: boolean };

export type SavedSummary = {
  id: string;
  label: string;            // the argument, not the uploader's headline
  title: string;
  subject: string;
  category: string;
  category_reason: string;
  platform: string;
  preview: string;
  notes_chars: number;
  created_at: number;
};

export type SavedNote = {
  id: string;
  title: string;
  subject: string;
  category: string;
  category_reason: string;
  platform: string;
  source: string;
  notes: string;
  transcript_chars: number;
  session: string;
};

export type NotesResult = {
  title: string;
  platform: string;
  source: string;
  notes: string;
  session: string;
  transcript_chars: number;
};

export type Finding = {
  title: string; url: string; platform: string;
  relation: "supports" | "disputes" | "context";
  quote: string; timestamp: string; note: string;
};

export type ResearchResult = {
  claim: string; queries: string[]; findings: Finding[];
  verdict: string; searched: number; read: number;
};

export type SearchHit = {
  title: string; url: string; uploader: string;
  duration: number | null; views: number | null; platform: string;
};

export type Job<T> = {
  id: string;
  status: "queued" | "running" | "done" | "error";
  step: string;
  result?: T;
  error?: string;
};

/* ── Auth ──────────────────────────────────────────────────── */

export const me = () => get<{ user: User | null; auth_required: boolean }>("/api/auth/me");
export const login = (email: string, password: string) =>
  post<{ user: User }>("/api/auth/login", { email, password });
export const register = (email: string, password: string, invite: string) =>
  post<{ user: User }>("/api/auth/register", { email, password, invite });
export async function logout() {
  await post("/api/auth/logout");
  await clearSession();
}

/* ── Library ───────────────────────────────────────────────── */

export const saved = () => get<{ notes: SavedSummary[]; count: number }>("/api/saved");
export const savedNote = (id: string) => get<SavedNote>(`/api/saved/${id}`);
export const deleteNote = (id: string) => del<{ deleted: string }>(`/api/saved/${id}`);
export const refile = () => post<{ moved: unknown[]; remaining: number }>("/api/saved/refile");
export const save = (body: {
  session?: string; notes?: string; title?: string; url?: string; topic?: string;
}) => post<{ saved: SavedSummary }>("/api/saved", body);

/* ── Search ────────────────────────────────────────────────── */

export const search = (query: string, limit = 8) =>
  post<{ results: SearchHit[]; count: number }>("/api/search", { query, limit });

/* ── Chat ──────────────────────────────────────────────────── */

export const ask = (body: {
  question: string; session?: string; notes?: string; title?: string;
  history?: { role: string; content: string }[];
}) => post<{ answer: string }>("/api/chat", body);

/* ── Jobs ──────────────────────────────────────────────────── */

export const job = <T,>(id: string) => get<Job<T>>(`/api/jobs/${id}`);

/**
 * Poll a job to completion.
 *
 * Backs off from ~1s toward 4s: notes usually land in seconds but can take
 * minutes, and hammering the server while it works helps nobody.
 */
export async function awaitJob<T>(
  id: string,
  onStep?: (step: string) => void,
  opts: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const deadline = Date.now() + (opts.timeoutMs ?? 10 * 60_000);
  let delay = 900;
  let lastStep = "";

  while (Date.now() < deadline) {
    if (opts.signal?.aborted) throw new ApiError("Cancelled", 0);
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 1.25, 4000);

    const j = await job<T>(id);
    if (j.step && j.step !== lastStep) {
      lastStep = j.step;
      onStep?.(j.step);
    }
    if (j.status === "done") {
      if (j.result === undefined) throw new ApiError("The job finished with no result.", 500);
      return j.result;
    }
    if (j.status === "error") throw new ApiError(j.error ?? "The job failed.", 500);
  }
  throw new ApiError("Timed out waiting for the job.", 504);
}

export async function makeNotes(
  body: { url: string; extra?: string },
  onStep?: (step: string) => void,
): Promise<NotesResult> {
  const { job: id } = await post<{ job: string }>("/api/notes", body);
  return awaitJob<NotesResult>(id, onStep);
}

export async function findSources(
  body: { claim: string; instruction?: string; session?: string },
  onStep?: (step: string) => void,
): Promise<ResearchResult> {
  const { job: id } = await post<{ job: string }>("/api/research", body);
  return awaitJob<ResearchResult>(id, onStep);
}

/** Is this a link, or a topic to search for? Mirrors the web app. */
export function looksLikeLink(v: string): boolean {
  const t = v.trim();
  if (!t || /\s/.test(t)) return false;
  if (/^[0-9A-Za-z_-]{11}$/.test(t)) return true;
  if (/^https?:\/\//i.test(t)) return true;
  return /^[\w-]+(\.[\w-]+)+(\/|$)/.test(t);
}
