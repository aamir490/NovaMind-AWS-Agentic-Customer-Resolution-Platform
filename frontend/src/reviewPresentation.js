// ReviewRequired.status describes the pause request, not the terminal proposal state.
export function reviewRequestStatusForDisplay(review, reviewedStatus) {
  return review && reviewedStatus === null ? review.status : null;
}
