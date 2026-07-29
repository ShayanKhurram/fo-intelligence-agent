// Failure taxonomy — micro_rag_plan.md §6. Every branch returns readable prose in the
// product's voice. No stack traces, no raw JSON, no empty states that just say
// "no results."

export function noMatchMessage(relaxedFilters: string[], candidateCount: number): string {
  if (relaxedFilters.length === 0) {
    return "No records match those filters, and there was nothing broader to relax them to.";
  }
  const relaxedList = relaxedFilters.join(", then ");
  return `No exact match for that combination. After relaxing ${relaxedList}, showing ${candidateCount} record(s) instead.`;
}

export function retrievalFloorMessage(): string {
  return "That's outside what this dataset covers — it holds a small, US-weighted set of family office records assembled from public filings. Try a question about entity type, state, AUM, or investing mandate.";
}

export function outOfScopeMessage(): string {
  return "That's outside what this dataset covers — it holds US family office records assembled from public filings (13F, 990-PF, Form 5500, Form D). Try: \"SFOs in Texas\" or \"who invests in growth equity\".";
}

export function entailmentDiscardedMessage(): string {
  return "We found relevant records but couldn't generate a fully-grounded summary this time. Here are the records directly — they're still useful even without a written answer.";
}

export function timeoutMessage(): string {
  return "Couldn't complete that search — try again.";
}

export function blankFieldMessage(entityName: string, field: string): string {
  return `We have ${entityName} but haven't confirmed its ${field.replace(/_/g, " ")}. Here's what we do have.`;
}
