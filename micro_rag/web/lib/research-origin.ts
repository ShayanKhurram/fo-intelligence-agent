// The research sweep runs on Render, not Vercel — Render has no per-request function
// timeout, so a sweep over ~480 organizations finishes in a single SSE connection instead
// of being sliced into 45s Vercel requests. This module points the client's three run
// calls (POST start, GET stream, DELETE stop) at the service origin.
//
// `NEXT_PUBLIC_*` is inlined at Next build time, so reading it at module scope in a
// client component is correct. When the variable is unset the function returns the path
// unchanged, which is exactly today's same-origin Vercel behaviour — that is the
// rollback path, so there is deliberately no fallback that guesses an origin.
const ORIGIN = (process.env.NEXT_PUBLIC_RESEARCH_ORIGIN || "").replace(/\/$/, "");

export function researchUrl(path: string): string {
  return ORIGIN ? `${ORIGIN}${path}` : path;
}