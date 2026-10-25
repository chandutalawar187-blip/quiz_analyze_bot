const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const path = require('node:path');
const source = name => readFileSync(path.join(__dirname, '../src/quizcord_bot/static', name), 'utf8');

function fixture(search = '') {
  const nodes = new Map();
  const node = () => ({children: [], listeners: {}, dataset: {}, value: '', hidden: false,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {}, focus() {}, querySelector() { return null; }, querySelectorAll() { return []; }});
  const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    createElement: node, body: node(), head: node()};
  const context = vm.createContext({document, URL, URLSearchParams, console, setTimeout, clearTimeout,
    location: {pathname: '/', search, origin: 'https://example.test', protocol: 'https:'}, window: {}});
  return {context, document, nodes};
}
const run = (f, code) => vm.runInContext(code, f.context);
const settle = () => new Promise(resolve => setImmediate(resolve));

for (const requested of ['older-session', null]) {
  test(`session initialization honors ${requested || 'newest fallback'} and supports paging`, async () => {
    const f = fixture(requested ? '?session=' + requested : '');
    const calls = [];
    const latest = Array.from({length: 50}, (_, i) => ({session_id: 'new-' + i, status: 'completed'}));
    f.context.fetch = async url => {
      calls.push(url);
      const data = url === '/api/sessions?offset=0' ? latest.slice() :
        url === '/api/sessions?offset=50' ? [{session_id: 'page-two', status: 'completed'}] :
        {session: {session_id: url.split('/').at(-1), status: 'completed'}};
      return {ok: true, json: async () => data};
    };
    run(f, source('admin-dashboard.js').split('    initializeGuildSelector().then(()=>{')[0]);
    run(f, 'render = summary => { globalThis.rendered = summary; };');
    await run(f, 'refresh()');
    assert.equal(f.context.rendered.session.session_id, requested || 'new-0');
    assert(f.nodes.get('session-select').children.some(option => option.value === (requested || 'new-0')));
    await f.nodes.get('sessions-older').listeners.click();
    await settle();
    assert(calls.includes('/api/sessions?offset=50'));
    assert.equal(f.context.rendered.session.session_id, 'page-two');
    assert.equal(f.nodes.get('sessions-older').disabled, true);
    f.nodes.get('sessions-newer').listeners.click();
    await settle();
    assert.equal(f.context.rendered.session.session_id, 'new-0');
  });
}

test('faculty list, search, add and remove explicitly use the displayed guild', async () => {
  const f = fixture();
  const calls = [];
  f.context.fetch = async (url, options) => { calls.push([url, options.method]); return {ok: true}; };
  run(f, source('faculty-dashboard.js').split('    initializeGuildSelector().then(()=>api(')[0]);
  await assert.rejects(run(f, 'api("/api/faculty/students")'), /Select a Discord server/);
  assert.equal(calls.length, 0);
  run(f, 'displayedGuildId = "guild-one";');
  // Another tab's session selection does not change the guild pinned to this page.
  f.document.getElementById('guild-select').value = 'guild-two';
  for (const [url, method] of [['/api/faculty/students', 'GET'], ['/api/faculty/candidates?search=USN', 'GET'],
    ['/api/faculty/students/USN', 'POST'], ['/api/faculty/students/USN', 'DELETE']]) {
    await run(f, `api(${JSON.stringify(url)}, {method: ${JSON.stringify(method)}})`);
    const [called, verb] = calls.at(-1);
    const parsed = new URL(called, 'https://example.test');
    assert.equal(parsed.searchParams.get('guild_id'), 'guild-one');
    assert.equal(verb, method);
    if (url.includes('search=')) assert.equal(parsed.searchParams.get('search'), 'USN');
  }
});

function replayFixture({pendingSdk = false} = {}) {
  const f = fixture();
  f.document.body.dataset = {sessionReplayEnabled: 'true', sessionReplayDsn: 'synthetic-dsn'};
  let cookie = 'quizcord_analytics_consent=accepted';
  Object.defineProperty(f.document, 'cookie', {get: () => cookie, set() { /* Simulate a rejected write. */ }});
  const events = [];
  let config;
  const sentry = {init(options) { config = options; }, browserTracingIntegration() {}, replayIntegration() {},
    getReplay() { return {start() { events.push('start'); }, async stop(options) {
      assert.equal(options.flush, false); events.push('stop');
    }}; }};
  if (!pendingSdk) f.context.window.Sentry = sentry;
  f.context.console = {warn() {}, error() {}};
  run(f, source('cookie-preferences.js'));
  return {...f, events, sentry, config: () => config};
}

test('failed decline persistence still stops Replay and blocks new and pending traces', async () => {
  const f = replayFixture();
  await settle();
  assert.deepEqual(f.events, ['start']);
  await f.nodes.get('cookie-preferences-reject').listeners.click();
  assert.deepEqual(f.events, ['start', 'stop']);
  assert.equal(f.config().tracesSampler(), 0);
  assert.equal(f.config().beforeSendTransaction({}), null);
  assert.match(f.nodes.get('cookie-preferences-message').textContent, /could not save.*may not survive a reload/);
  f.nodes.get('cookie-preferences').listeners.click();
  assert.equal(f.nodes.get('cookie-analytics-enabled').checked, false);
});

test('declining during SDK loading prevents Replay from starting even if the cookie stays accepted', async () => {
  const f = replayFixture({pendingSdk: true});
  await f.nodes.get('cookie-preferences-reject').listeners.click();
  f.context.window.Sentry = f.sentry;
  f.document.head.children[0].onload();
  await settle();
  assert.deepEqual(f.events, []);
});
