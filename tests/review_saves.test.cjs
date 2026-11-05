const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

// Exercise the actual save/action handlers with a controlled network, without a browser dependency.
const source = fs.readFileSync('static/review-document.js', 'utf8');
const saving = source.slice(source.indexOf('  let saveChain ='), source.indexOf('  async function addRow'));
const actions = source.slice(source.indexOf('  const LEAVING_ACTIONS'), source.indexOf('  function showShortcuts'));
function harness() {
  const status = { textContent: '' };
  const requests = [];
  const messages = [];
  const context = {
    state: { doc: { fields: {}, tables: {}, file_name: 'invoice.pdf' }, saving: 0, saveFailed: false },
    drafts: new Map(),
    urls: { api: '/api/documents/1', list: '/review/documents' },
    editable: () => true,
    $: () => status,
    refresh: () => {},
    labelFor: path => path,
    toast: message => messages.push(message),
    focusPath: () => {},
    moveOn: () => { context.moves++; },
    moves: 0,
    document: { activeElement: null },
    fieldsForm: { contains: () => false },
    window: { location: { href: '' } },
    flashNext: () => {},
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
  };
  context.stateFor = path => context.state.doc.fields[path] || {};
  vm.createContext(context);
  vm.runInContext(saving + actions + '\nthis.handlers = {saveValue, acceptValue, runAction};', context);
  return { context, status, requests, messages, ...context.handlers };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('queued saves are counted immediately and run in order', async () => {
  const h = harness();
  const a = h.saveValue('a', { value: 'first' });
  const b = h.saveValue('b', { value: 'second' });
  assert.equal(h.context.state.saving, 2);
  assert.equal(h.status.textContent, 'Saving…');
  await tick();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve({ fields: { a: { value: 'first' } } });
  await a;
  await tick();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ fields: { a: { value: 'first' }, b: { value: 'second' } } });
  await b;
  assert.equal(h.context.state.saving, 0);
  assert.equal(h.status.textContent, 'All changes saved');
});

test('a different successful save cannot hide an earlier failure or allow confirmation', async () => {
  const h = harness();
  const failed = h.saveValue('a', { value: 'keep this draft' });
  await tick();
  h.requests[0].reject(new Error('offline'));
  assert.equal(await failed, false);
  const successful = h.saveValue('b', { value: 'saved' });
  await tick();
  h.requests[1].resolve({ fields: { b: { value: 'saved' } } });
  await successful;
  assert.equal(h.context.state.saveFailed, true);
  assert.equal(h.context.drafts.get('a').value, 'keep this draft');
  assert.equal(h.status.textContent, 'Some changes were not saved');
  await h.runAction('confirm');
  assert.equal(h.requests.length, 2);
  assert.equal(h.context.window.location.href, '');
  assert.match(h.messages.at(-1), /Press Enter/);
});

test('retry clears the failure and permits confirmation after saving', async () => {
  const h = harness();
  const failed = h.saveValue('a', { value: 'edited' });
  await tick();
  h.requests[0].reject(new Error('offline'));
  await failed;
  const retry = h.acceptValue('a', { value: 'edited' });
  await tick();
  h.requests[1].resolve({ fields: { a: { value: 'edited', validated: true } } });
  await retry;
  assert.equal(h.context.state.saveFailed, false);
  assert.equal(h.context.drafts.size, 0);
  assert.equal(h.context.moves, 1);
  const confirm = h.runAction('confirm');
  await tick();
  assert.match(h.requests[2].url, /actions\/confirm$/);
  h.requests[2].resolve({});
  await confirm;
  assert.equal(h.context.window.location.href, '/review/documents');
});

test('a failed Enter save keeps focus on the field', async () => {
  const h = harness();
  const save = h.acceptValue('a', { value: 'edited' });
  await tick();
  h.requests[0].reject(new Error('offline'));
  await save;
  assert.equal(h.context.moves, 0);
});

test('an older response never clears a newer draft for the same field', async () => {
  const h = harness();
  const first = h.saveValue('a', { value: 'first' });
  const second = h.saveValue('a', { value: 'second' });
  await tick();
  h.requests[0].resolve({ fields: { a: { value: 'first' } } });
  await first;
  assert.equal(h.context.drafts.get('a').value, 'second');
  await tick();
  h.requests[1].resolve({ fields: { a: { value: 'second' } } });
  await second;
  assert.equal(h.context.drafts.size, 0);
});
