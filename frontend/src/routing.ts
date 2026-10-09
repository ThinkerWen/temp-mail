import { useCallback, useEffect, useRef, useState } from 'react';

export const sectionPaths = {
  inbox: '/inbox',
  providers: '/providers',
  operations: '/operations',
  settings: '/settings',
} as const;
export type Section = keyof typeof sectionPaths;

const positionKey = '__tempMailPosition';

function readPosition(): number | null {
  const value: unknown = window.history.state?.[positionKey];
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
}

function historyState(position: number) {
  const existing: unknown = window.history.state;
  return {
    ...(existing && typeof existing === 'object' ? existing : {}),
    [positionKey]: position,
  };
}

function sectionUrl(section: Section) {
  return sectionPaths[section] + window.location.search + window.location.hash;
}

function canNavigate() {
  return window.dispatchEvent(new Event('temp-mail:navigate', { cancelable: true }));
}

function readSection(): Section {
  const path = window.location.pathname.replace(/\/$/, '');
  return (
    (Object.keys(sectionPaths) as Section[]).find((section) => sectionPaths[section] === path) ?? 'inbox'
  );
}

export function useSection() {
  const [section, setSection] = useState(readSection);
  const current = useRef({ section, position: readPosition() ?? 0, url: sectionUrl(section) });
  const restoring = useRef(false);

  useEffect(() => {
    const initial = current.current;
    window.history.replaceState(historyState(initial.position), '', initial.url);

    const sync = () => {
      const previous = current.current;
      const next = readSection();
      const position = readPosition();
      if (restoring.current) {
        // A cancelled Back/Forward returns to the existing entry without adding history.
        if (position === previous.position) {
          restoring.current = false;
          return;
        }
        if (position !== null) {
          window.history.go(previous.position - position);
          return;
        }
        restoring.current = false;
      }

      if (next !== previous.section && !canNavigate()) {
        if (position !== null && position !== previous.position) {
          restoring.current = true;
          window.history.go(previous.position - position);
        } else {
          // Unindexed same-document entries (for example manual hash changes)
          // cannot reveal their history delta. Keep the visible URL and form aligned.
          window.history.pushState(historyState(previous.position), '', previous.url);
        }
        return;
      }

      const accepted = { section: next, position: position ?? previous.position + 1, url: sectionUrl(next) };
      current.current = accepted;
      window.history.replaceState(historyState(accepted.position), '', accepted.url);
      setSection(next);
    };
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);

  const navigate = useCallback((next: Section) => {
    if (restoring.current) return;
    if (window.location.pathname !== sectionPaths[next]) {
      if (!canNavigate()) return;
      const position = current.current.position + 1;
      window.history.pushState(historyState(position), '', sectionPaths[next]);
      current.current = { section: next, position, url: sectionPaths[next] };
    }
    setSection(next);
  }, []);

  return [section, navigate] as const;
}
