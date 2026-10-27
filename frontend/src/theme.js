export const THEME_STORAGE_KEY = 'novamind-theme';
export const THEME_PREFERENCES = Object.freeze(['dark', 'light', 'system']);

export function normalizeTheme(value) {
  return THEME_PREFERENCES.includes(value) ? value : 'system';
}

export function resolveTheme(preference, systemDark = false) {
  const normalized = normalizeTheme(preference);
  return normalized === 'system' ? (systemDark ? 'dark' : 'light') : normalized;
}

export function readThemePreference(storage) {
  try {
    const target = storage ?? globalThis.localStorage;
    return normalizeTheme(target?.getItem(THEME_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

export function storeThemePreference(preference, storage) {
  const normalized = normalizeTheme(preference);
  try {
    const target = storage ?? globalThis.localStorage;
    target?.setItem(THEME_STORAGE_KEY, normalized);
  } catch {
    // A blocked storage API must not prevent the theme from applying in memory.
  }
  return normalized;
}

export function applyTheme(theme, documentRef = globalThis.document) {
  const resolved = theme === 'light' ? 'light' : 'dark';
  const root = documentRef?.documentElement;
  if (!root) return resolved;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  const themeColor = documentRef.querySelector?.('meta[name="theme-color"]');
  themeColor?.setAttribute('content', resolved === 'dark' ? '#0b1018' : '#f4f7fb');
  return resolved;
}
