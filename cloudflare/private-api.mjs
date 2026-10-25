export function isPrivateApiRequest(request) {
  const url = new URL(request.url);
  if (url.search) return false;
  if (request.method === 'POST') return /^\/api\/(info|download)$/.test(url.pathname);
  return request.method === 'GET' && /^\/api\/(status|file)\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(url.pathname);
}
