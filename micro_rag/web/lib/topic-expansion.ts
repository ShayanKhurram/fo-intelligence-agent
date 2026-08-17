// T49.2 — topic expansion for the lexical half of retrieval.
//
// Why this exists, measured: this corpus contains 6 records that genuinely concern climate —
// CHERRY CREEK ("Energy transition wealth creates a distinctive investment thesis"), THE DOWLING
// GROUP ("climate change"), TAFT ("sustainable communities"), COX ("sustainable stewardship"),
// ARI GROUP, COYLE. Ranking on the embedded topic "climate" put them at positions 32, 37, 45, 73
// and 115 of 176, because MiniLM does not connect "climate" to "energy transition" or
// "sustainable" on these short, boilerplate-heavy chunks. Meanwhile a plain keyword scan for
// climate/clean-energy/decarbonization/renewable/sustainable found all 6 exactly.
//
// So the division of labour is: the LLM supplies vocabulary (what it is good at), Postgres FTS
// does the matching (what it is good at), and the T49.3 judge confirms relevance at the end.
// One cheap call, and it only has to name words — no reasoning, no reading of records.
//
// FAILS OPEN: any error, or an unusable reply, returns just the original topic, which is exactly
// today's behaviour. Expansion can only ever ADD search vocabulary.

import { ollamaChat, type ChatMessage } from "./ollama.ts";

const MAX_TERMS = 8;

const SYSTEM = `You expand a search topic into the words that would actually appear in text about
it. Reply with a comma-separated list of up to ${MAX_TERMS} short terms, most important first.

Include the original topic, common synonyms, and the sector vocabulary a firm would use to
describe this kind of work. Prefer single words or two-word phrases. Do not include company
names, and do not explain yourself.

Example — topic: climate
climate, clean energy, decarbonization, renewable, sustainability, energy transition, cleantech, carbon`;

/** Splits a model reply into usable search terms. Rejects anything that looks like prose
 * rather than a term list, so a chatty reply degrades to no expansion instead of garbage. */
export function parseTerms(raw: string, topic: string): string[] {
  if (!raw) return [topic];
  // A reply containing sentence punctuation is prose, not a list.
  const firstLine = raw.split("\n").map((l) => l.trim()).filter(Boolean).find((l) => l.includes(",")) ?? raw;
  const terms = firstLine
    .split(",")
    .map((t) => t.trim().toLowerCase().replace(/^[-*\d.\s]+/, ""))
    // A term is one or two plain words. Anything longer is a sentence fragment.
    .filter((t) => t.length > 1 && t.length <= 30 && /^[a-z][a-z\s-]*$/.test(t) && t.split(/\s+/).length <= 2);

  const unique = [...new Set([topic.toLowerCase(), ...terms])].slice(0, MAX_TERMS);
  return unique.length > 0 ? unique : [topic];
}

export async function expandTopic(topic: string): Promise<string[]> {
  const trimmed = topic.trim();
  if (!trimmed) return [];
  // A multi-word thesis is usually already specific ("real estate"), and a name lookup
  // ("kapor") must NOT be expanded — synonyms of a company name are other companies.
  if (trimmed.split(/\s+/).length > 2) return [trimmed];

  const messages: ChatMessage[] = [
    { role: "system", content: SYSTEM },
    { role: "user", content: `topic: ${trimmed}` },
  ];
  try {
    // Cheapest tier: this is vocabulary recall, not reasoning, and it sits in the critical path
    // of a route capped at maxDuration = 60.
    const raw = await ollamaChat(messages, "cheapest");
    return parseTerms(raw, trimmed);
  } catch {
    return [trimmed]; // fail open — no expansion, today's behaviour
  }
}
