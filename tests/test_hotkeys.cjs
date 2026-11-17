const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(resolve(__dirname, '../web/app.js'), 'utf8');

function addProfile(usedKeys) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { addEventListener() {}, focus() {}, select() {} });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: () => [] },
    location: { hash: '' },
    sessionStorage: { getItem() {}, setItem() {} },
    history: { replaceState() {} },
    setInterval() {},
    fetch: () => new Promise(() => {}),
    crypto: { randomUUID: () => 'new-profile' },
  });
  vm.runInContext(source, context);
  context.fixture = {
    pause_hotkey: 'F8', stop_hotkey: 'F9',
    profiles: usedKeys.map((hotkey, i) => ({
      id: `p${i}`, hotkey, clipboard_hotkey: '', language: 'C++17',
    })),
  };
  vm.runInContext('config = fixture; collect = () => {}; render = () => {}; changed = () => {};', context);
  element('add-profile').onclick();
  return vm.runInContext('config.profiles.at(-1)', context);
}

test('new screenshot binding skips reserved F12', () => {
  const profile = addProfile(['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F10', 'F11']);
  assert.equal(profile.hotkey, 'F13');
  assert.equal(profile.clipboard_hotkey, 'F14');
});

test('new clipboard binding skips reserved F12', () => {
  const profile = addProfile(['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F10']);
  assert.equal(profile.hotkey, 'F11');
  assert.equal(profile.clipboard_hotkey, 'F13');
});

test('new clipboard binding stays disabled when only F12 remains', () => {
  const usedKeys = Array.from({ length: 23 }, (_, i) => `F${i + 1}`)
    .filter(key => !['F8', 'F9', 'F12'].includes(key));
  const profile = addProfile(usedKeys);
  assert.equal(profile.hotkey, 'F24');
  assert.equal(profile.clipboard_hotkey, '');
});
