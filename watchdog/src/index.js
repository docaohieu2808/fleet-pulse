// Dead man's switch for the monitoring stack on server-3.
//
// Prometheus fires an always-on "Watchdog" alert; Alertmanager re-sends it here every 5 minutes. A firing Watchdog
// notification proves Prometheus, Alertmanager, server-3 and its network are all alive. If none arrives for
// SILENCE_S, this Worker (outside the fleet) sends one Telegram message. It has no page: the owner only ever sees
// it as that message.
//
// Only a *firing* Watchdog counts: when Prometheus dies, Alertmanager eventually sends "resolved" for it, and that
// must not reset the clock. A Worker that never received any heartbeat (wrong token, wiped table) is not silent
// either: it is armed on its first cron run and alerts SILENCE_S later.
//
// No "recovered" message, matching the critical Telegram channel: Fleet Pulse shows the rest.
export const SILENCE_S = 10 * 60;

const nowS = () => Math.floor(Date.now() / 1000);

// One-row-per-key table in D1: last_heartbeat, armed_at (unix seconds), alerted ("1" once Telegram went out).
async function get(env, key) {
  const row = await env.DB.prepare('SELECT value FROM state WHERE key = ?').bind(key).first();
  return row ? row.value : null;
}
const put = (env, key, value) =>
  env.DB.prepare('INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .bind(key, String(value)).run();
const del = (env, key) => env.DB.prepare('DELETE FROM state WHERE key = ?').bind(key).run();

export async function telegram(env, text) {
  try {
    const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true }),
    });
    if (!res.ok) console.error(`telegram answered ${res.status}: ${(await res.text()).slice(0, 200)}`);
    return res.ok;
  } catch (err) {
    console.error(`telegram unreachable: ${err}`);
    return false;
  }
}

// Constant-time comparison: the endpoint is public, the token must not leak through response timing.
export function sameToken(a, b) {
  const x = new TextEncoder().encode(a || '');
  const y = new TextEncoder().encode(b || '');
  if (x.length !== y.length || x.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function authorized(request, env) {
  const token = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  return sameToken(token, env.HEARTBEAT_TOKEN);
}

// True when the Alertmanager webhook body carries a firing Watchdog alert.
export function isHeartbeat(body) {
  return Array.isArray(body?.alerts) &&
    body.alerts.some((a) => a?.status === 'firing' && a?.labels?.alertname === 'Watchdog');
}

// Decide what the cron run should do. Pure, so it can be tested without D1 or Telegram.
export function verdict({ now, lastHeartbeat, armedAt, alerted }) {
  const since = lastHeartbeat || armedAt;
  if (!since) return { arm: true };
  const silent = now - since;
  if (silent < SILENCE_S || alerted) return {};
  return {
    alert: lastHeartbeat
      ? `Watchdog · Giám sát · ${Math.round(silent / 60)} phút không nhận được nhịp tim từ Alertmanager (server-3).\n` +
        'server-3, Prometheus hoặc Alertmanager có thể đã chết: mọi cảnh báo khác đang mù.'
      : `Watchdog · Giám sát · chưa từng nhận nhịp tim nào sau ${Math.round(silent / 60)} phút kể từ khi bật.\n` +
        'Kiểm tra route heartbeat trong Alertmanager và HEARTBEAT_TOKEN.',
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/heartbeat' && request.method === 'POST') {
      if (!authorized(request, env)) return new Response('unauthorized', { status: 401 });
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response('invalid json', { status: 400 });
      }
      if (!isHeartbeat(body)) return new Response('ignored: not a firing Watchdog alert', { status: 202 });
      await put(env, 'last_heartbeat', nowS());
      await del(env, 'alerted');
      return new Response('ok');
    }
    // Proves the Telegram wiring without waiting for an outage.
    if (url.pathname === '/test' && request.method === 'POST') {
      if (!authorized(request, env)) return new Response('unauthorized', { status: 401 });
      const sent = await telegram(env, 'Watchdog · Tin thử từ watchdog.docaohieu.com. Không cần làm gì.');
      return Response.json({ sent, last_heartbeat: Number(await get(env, 'last_heartbeat')) || null });
    }
    return new Response('not found', { status: 404 });
  },

  async scheduled(_event, env) {
    const v = verdict({
      now: nowS(),
      lastHeartbeat: Number(await get(env, 'last_heartbeat')) || 0,
      armedAt: Number(await get(env, 'armed_at')) || 0,
      alerted: Boolean(await get(env, 'alerted')),
    });
    if (v.arm) await put(env, 'armed_at', nowS());
    if (v.alert && (await telegram(env, v.alert))) await put(env, 'alerted', 1);
  },
};
