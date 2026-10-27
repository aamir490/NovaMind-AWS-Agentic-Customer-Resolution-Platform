// Pure prefill rules keep draft loading advisory and preserve an Admin's edits.
export const draftAction = {
  REQUEST_EVIDENCE: 'REQUEST_MORE_EVIDENCE',
  ESCALATE: 'ESCALATE',
};

export function resolutionDraftFingerprint(draft) {
  if (!draft) return null;
  return `${draft.recommendedDecision}\u0000${draft.decisionReason}\u0000${draft.customerFacingResponse}`;
}
export function resolutionDraftPrefill(draft, options, {
  hasRecordedResolution = false, edited = false, previousFingerprint = null,
} = {}) {
  const action = draft ? draftAction[draft.recommendedDecision] : null;
  const fingerprint = resolutionDraftFingerprint(draft);
  if (!draft || !action || !options.includes(action) || hasRecordedResolution || edited
      || previousFingerprint === fingerprint) return null;
  return {
    action,
    reason: draft.decisionReason,
    customerMessage: draft.customerFacingResponse,
    fingerprint,
  };
}
