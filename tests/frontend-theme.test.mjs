import test from 'node:test';
import assert from 'node:assert/strict';

import {
  THEME_STORAGE_KEY,
  applyTheme,
  normalizeTheme,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
} from '../frontend/src/theme.js';

test('theme preferences are closed to dark, light, and system', () => {
  assert.equal(normalizeTheme('dark'), 'dark');
  assert.equal(normalizeTheme('light'), 'light');
  assert.equal(normalizeTheme('system'), 'system');
  assert.equal(normalizeTheme('unexpected'), 'system');
  assert.equal(normalizeTheme(null), 'system');
});

test('system theme resolves from the current operating-system preference', () => {
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('light', true), 'light');
});

test('theme storage persists only normalized preferences and fails safely', () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key),
    setItem: (key, value) => values.set(key, value),
  };
  assert.equal(storeThemePreference('dark', storage), 'dark');
  assert.equal(values.get(THEME_STORAGE_KEY), 'dark');
  values.set(THEME_STORAGE_KEY, 'invalid');
  assert.equal(readThemePreference(storage), 'system');
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  assert.equal(readThemePreference(blocked), 'system');
  assert.equal(storeThemePreference('light', blocked), 'light');
});

test('applying a theme updates the document root and browser theme color', () => {
  const attributes = new Map();
  const meta = { setAttribute: (name, value) => attributes.set(name, value) };
  const documentRef = {
    documentElement: { dataset: {}, style: {} },
    querySelector: (selector) => selector === 'meta[name="theme-color"]' ? meta : null,
  };
  assert.equal(applyTheme('light', documentRef), 'light');
  assert.equal(documentRef.documentElement.dataset.theme, 'light');
  assert.equal(documentRef.documentElement.style.colorScheme, 'light');
  assert.equal(attributes.get('content'), '#f4f7fb');
  assert.equal(applyTheme('other', documentRef), 'dark');
  assert.equal(attributes.get('content'), '#0b1018');
});
