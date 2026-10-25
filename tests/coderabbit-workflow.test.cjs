const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const yaml = readFileSync('.github/workflows/coderabbit-review.yml', 'utf8');
const script = yaml.split('          script: |\n').at(-1).split('\n').map(line => line.slice(12)).join('\n');
const execute = new (Object.getPrototypeOf(async function(){}).constructor)('github', 'context', 'core', script);
const old = '2000-01-01T00:00:00Z';
const later = '2000-01-01T00:01:00Z';
const request = (id = 1, author = 42, attempt = 0) => ({id, user: {id: author}, created_at: old,
  body: `<!-- coderabbit-review sha=abc123 retry=${attempt} -->`});
const notice = () => ({id: 2, user: {login: 'coderabbitai[bot]'}, submitted_at: later,
  body: 'included review limit is currently reached; next included review will be available in 1 minute'});

async function simulate({channel = 'reviews', attempt = 0, spoofOnly = false, changed = false,
  duplicate = false, future = false, eventName = 'schedule'} = {}) {
  const comments = [request(1, spoofOnly ? 99 : 42, attempt),
    {...request(9, 99, 3), created_at: '2000-01-01T00:00:30Z'}];
  const botNotice = notice();
  if (future) botNotice.submitted_at = '2999-01-01T00:01:00Z';
  const pull = {number: 12, state: 'open', head: {sha: 'abc123'}, head_repository: {fork: true}};
  const writes = [];
  const reads = [];
  let issueReads = 0;
  const github = {rest: {
    users: {getAuthenticated: async () => ({data: {id: 42}})},
    pulls: {list: 'pulls', listReviews: 'reviews', listReviewComments: 'reviewComments',
      get: async () => ({data: changed ? {...pull, head: {sha: 'new'}} : pull})},
    issues: {listComments: 'comments', createComment: async payload => writes.push(payload)},
  }, paginate: async (endpoint) => {
    reads.push(endpoint);
    if (endpoint === 'pulls') return [pull];
    if (endpoint === 'comments') {
      issueReads++;
      return [...comments, ...(channel === 'comments' ? [botNotice] : []),
        ...(duplicate && issueReads > 1 ? [{...request(3, 42, 1), created_at: later}] : [])];
    }
    return endpoint === channel ? [botNotice] : [];
  }};
  await execute(github, {repo: {owner: 'test', repo: 'test'}, eventName, payload: {issue: {number: 12}}},
    {info() {}, warning() {}});
  return {writes, reads};
}

test('retry triggers run in trusted contexts without checking out PR code', () => {
  assert.match(yaml, /schedule:\n\s+- cron:/);
  assert.doesNotMatch(yaml, /\n  pull_request_review(?:_comment)?:/);
  assert.doesNotMatch(yaml, /actions\/checkout|download-artifact|eval\(/);
});
for (const channel of ['comments', 'reviewComments', 'reviews']) {
  test(`polling handles fork notices in ${channel} and ignores participant markers`, async () => {
    const {writes, reads} = await simulate({channel});
    assert.equal(writes.length, 1);
    assert.match(writes[0].body, /sha=abc123 retry=1/);
    assert(reads.includes('pulls'));
  });
}
for (const scenario of [{spoofOnly: true}, {attempt: 3}, {changed: true}, {duplicate: true}, {future: true}]) {
  test(`skips unsafe or premature retry ${JSON.stringify(scenario)}`, async () => {
    assert.equal((await simulate(scenario)).writes.length, 0);
  });
}
test('trusted issue comments use the same retry rules', async () => {
  assert.equal((await simulate({channel: 'comments', eventName: 'issue_comment'})).writes.length, 1);
});
