import test from 'node:test';
import assert from 'node:assert/strict';
import { isPrivateApiRequest } from './private-api.mjs';

test('private entrypoint admits only downloader API methods and paths', () => {
  const request = (path, method='GET') => new Request(`https://reclip.internal${path}`, {method});
  assert.equal(isPrivateApiRequest(request('/api/info','POST')), true);
  assert.equal(isPrivateApiRequest(request('/api/download','POST')), true);
  assert.equal(isPrivateApiRequest(request('/api/file/01234567-89ab-4def-8123-456789abcdef')), true);
  for (const path of ['/', '/admin', '/api/file/../../admin', '/api/info?unexpected=1']) {
    assert.equal(isPrivateApiRequest(request(path)), false);
  }
  assert.equal(isPrivateApiRequest(request('/api/info')), false);
  assert.equal(isPrivateApiRequest(request('/api/download','DELETE')), false);
});
