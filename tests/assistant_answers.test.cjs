const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

// The answer parser needs no browser: load it from the real script and check what it would put on the page.
const source = fs.readFileSync('static/assistant.js', 'utf8');
const parsing = source.slice(source.indexOf('  function parseAnswer'), source.indexOf('  function buildAnswer'));
const context = {};
vm.createContext(context);
vm.runInContext(parsing + '\nthis.parseAnswer = parseAnswer;', context);
// Round-trip through JSON so objects made inside the sandbox compare equal to ones made here.
const parse = text => JSON.parse(JSON.stringify(context.parseAnswer(text)));

test('lines become paragraphs and lists', () => {
  assert.deepEqual(parse('Two ways:\n\n- Upload a **new** master\n- Restore a backup\n\n1. First\n2. Second\nThat is all.'), [
    { type: 'paragraph', lines: [[{ text: 'Two ways:' }]] },
    { type: 'list', ordered: false, items: [[{ text: 'Upload a ' }, { text: 'new', bold: true }, { text: ' master' }], [{ text: 'Restore a backup' }]] },
    { type: 'list', ordered: true, items: [[{ text: 'First' }], [{ text: 'Second' }]] },
    { type: 'paragraph', lines: [[{ text: 'That is all.' }]] },
  ]);
});

test('markup in an answer stays plain text', () => {
  assert.deepEqual(parse('<img src=x onerror="alert(1)"> <b>hi</b>'), [
    { type: 'paragraph', lines: [[{ text: '<img src=x onerror="alert(1)"> <b>hi</b>' }]] },
  ]);
});

test('only links to pages on this website stay links', () => {
  const [paragraph] = parse('[Review](/review/documents?tab=confirmed) [A](https://evil.example) [B](javascript:alert(1)) [C](//evil.example) [D](/\\evil.example)');
  const links = paragraph.lines[0].filter(part => part.href);
  assert.deepEqual(links, [{ text: 'Review', href: '/review/documents?tab=confirmed' }]);
  assert.deepEqual(paragraph.lines[0].filter(part => part.text.trim()).map(part => part.text), ['Review', 'A', 'B', 'C', 'D']);
});

test('headings lose their markers', () => {
  assert.deepEqual(parse('## Backups\nSee the list.'), [
    { type: 'paragraph', lines: [[{ text: 'Backups' }], [{ text: 'See the list.' }]] },
  ]);
});
