import test from 'node:test';
import assert from 'node:assert/strict';
import { validateMutationRequest } from './request-security.mjs';

const url = 'https://reclip-cloudflare-preview.swyxio.workers.dev/api/download';
const check = headers => validateMutationRequest(new Request(url, {method:'POST', headers}));

test('allows the same-origin JSON UI and non-browser JSON clients', () => {
  assert.equal(check({'Content-Type':'application/json', Origin:new URL(url).origin, 'Sec-Fetch-Site':'same-origin'}), null);
  assert.equal(check({'Content-Type':'application/json; charset=utf-8'}), null);
});

test('rejects credentialed cross-site forms and JSON requests', () => {
  assert.equal(check({'Content-Type':'text/plain', Origin:'https://attacker.example'}).status, 415);
  assert.equal(check({'Content-Type':'application/json', Origin:'https://attacker.example'}).status, 403);
  assert.equal(check({'Content-Type':'application/json', Origin:'null'}).status, 403);
  assert.equal(check({'Content-Type':'application/json', 'Sec-Fetch-Site':'cross-site'}).status, 403);
});
