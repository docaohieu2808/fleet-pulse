// node --test watchdog/test/watchdog.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import { SILENCE_S, isHeartbeat, sameToken, verdict } from '../src/index.js';

const firing = { alerts: [{ status: 'firing', labels: { alertname: 'Watchdog' } }] };
const resolved = { alerts: [{ status: 'resolved', labels: { alertname: 'Watchdog' } }] };

test('only a firing Watchdog alert is a heartbeat', () => {
  assert.equal(isHeartbeat(firing), true);
  assert.equal(isHeartbeat(resolved), false); // Prometheus died, Alertmanager resolved it: not proof of life
  assert.equal(isHeartbeat({ alerts: [{ status: 'firing', labels: { alertname: 'Other' } }] }), false);
  assert.equal(isHeartbeat({}), false);
});

test('silence longer than the limit alerts once', () => {
  const now = 100_000;
  assert.deepEqual(verdict({ now, lastHeartbeat: now - 60, armedAt: 0, alerted: false }), {});
  assert.ok(verdict({ now, lastHeartbeat: now - SILENCE_S, armedAt: 0, alerted: false }).alert);
  assert.deepEqual(verdict({ now, lastHeartbeat: now - SILENCE_S, armedAt: 0, alerted: true }), {});
});

test('a Worker that never got a heartbeat arms itself, then alerts', () => {
  const now = 100_000;
  assert.deepEqual(verdict({ now, lastHeartbeat: 0, armedAt: 0, alerted: false }), { arm: true });
  assert.deepEqual(verdict({ now, lastHeartbeat: 0, armedAt: now - 60, alerted: false }), {});
  assert.match(verdict({ now, lastHeartbeat: 0, armedAt: now - SILENCE_S, alerted: false }).alert, /chưa từng/);
});

test('token comparison', () => {
  assert.equal(sameToken('abc', 'abc'), true);
  assert.equal(sameToken('abc', 'abd'), false);
  assert.equal(sameToken('', ''), false);
  assert.equal(sameToken('abc', undefined), false);
});
