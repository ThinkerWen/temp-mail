import { useSyncExternalStore } from 'react';

export type Theme = 'light' | 'dark';
export type Locale = 'zh' | 'en';
type Preferences = { theme: Theme; locale: Locale };

function readPreference(key: string) {
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

let preferences: Preferences = {
  theme: readPreference('temp-mail.theme') === 'dark' ? 'dark' : 'light',
  locale: readPreference('temp-mail.locale') === 'en' ? 'en' : 'zh',
};
const listeners = new Set<() => void>();
let initialized = false;

export function getLocale(): Locale {
  return preferences.locale;
}

export function t(zh: string, en: string): string {
  return preferences.locale === 'en' ? en : zh;
}

function applyPreferences() {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.toggle('dark', preferences.theme === 'dark');
  root.classList.toggle('light', preferences.theme === 'light');
  root.dataset.theme = preferences.theme;
  root.style.colorScheme = preferences.theme;
  root.lang = preferences.locale === 'zh' ? 'zh-CN' : 'en';
  document.title = t('Temp Mail · 临时邮箱', 'Temp Mail · Temporary email');
  document
    .querySelector('meta[name="description"]')
    ?.setAttribute(
      'content',
      t(
        '统一管理临时邮箱供应商、收件与操作记录的本地工作台。',
        'A local workspace for temporary email providers, inboxes, and operations.',
      ),
    );
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', preferences.theme === 'dark' ? '#1b1917' : '#fafaf9');
}

function update(next: Preferences) {
  if (next.theme === preferences.theme && next.locale === preferences.locale) return;
  preferences = next;
  applyPreferences();
  listeners.forEach((listener) => listener());
}

function persist(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Preferences still apply for this tab when browser storage is unavailable.
  }
}

function setTheme(theme: Theme) {
  if (theme !== 'light' && theme !== 'dark') return;
  persist('temp-mail.theme', theme);
  update({ ...preferences, theme });
}

function setLocale(locale: Locale) {
  if (locale !== 'zh' && locale !== 'en') return;
  persist('temp-mail.locale', locale);
  update({ ...preferences, locale });
}

export function initializePreferences() {
  applyPreferences();
  if (initialized || typeof window === 'undefined') return;
  initialized = true;
  window.addEventListener('storage', (event) => {
    if (event.key !== null && event.key !== 'temp-mail.theme' && event.key !== 'temp-mail.locale') return;
    // Read both values together, including removal and localStorage.clear().
    update({
      theme: readPreference('temp-mail.theme') === 'dark' ? 'dark' : 'light',
      locale: readPreference('temp-mail.locale') === 'en' ? 'en' : 'zh',
    });
  });
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getSnapshot = () => preferences;
const serverPreferences: Preferences = { theme: 'light', locale: 'zh' };

export function usePreferences() {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, () => serverPreferences);
  return { ...snapshot, setTheme, setLocale };
}

export function useI18n() {
  useSyncExternalStore(subscribe, getLocale, () => 'zh' as const);
  return t;
}
