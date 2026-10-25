// Basic authentication is cached by browsers, so mutation endpoints must also
// reject cross-origin form submissions that would otherwise carry credentials.
export function validateMutationRequest(request) {
  const contentType = request.headers.get('content-type') || '';
  if (!/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
    return { status: 415, error: 'Content-Type must be application/json' };
  }
  const origin = request.headers.get('origin');
  if (origin && origin !== new URL(request.url).origin) {
    return { status: 403, error: 'Cross-origin requests are not allowed' };
  }
  const fetchSite = request.headers.get('sec-fetch-site');
  if (fetchSite && fetchSite !== 'same-origin') {
    return { status: 403, error: 'Cross-origin requests are not allowed' };
  }
  return null;
}
