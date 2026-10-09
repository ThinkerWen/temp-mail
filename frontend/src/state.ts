import { useCallback, useEffect, useRef, useState } from 'react';
import { getLocale, t } from './preferences';

const PREFIX = 'temp-mail.';
export function readSession<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(sessionStorage.getItem(PREFIX + key) ?? 'null') ?? fallback;
  } catch {
    return fallback;
  }
}
export function writeSession(key: string, value: unknown) {
  try {
    if (value === null) sessionStorage.removeItem(PREFIX + key);
    else sessionStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* The page remains usable when browser storage is unavailable. */
  }
}
export function clearSession() {
  for (const key of ['token', 'operations', 'draft']) writeSession(key, null);
}

// A changed key never exposes the previous mailbox's data. Requests are serial,
// cancelled on selection changes, and paused while the tab is hidden.
export function useResource<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>, interval = 0) {
  const loader = useRef(load);
  loader.current = load;
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<{ key: string | null; data?: T; error?: unknown; loading: boolean }>({
    key: null,
    loading: false,
  });
  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      setState((current) => ({
        key,
        data: current.key === key ? current.data : undefined,
        error: current.key === key ? current.error : undefined,
        loading: true,
      }));
      try {
        const data = await loader.current(controller.signal);
        if (!controller.signal.aborted) setState({ key, data, loading: false });
      } catch (error) {
        if (!controller.signal.aborted) setState((current) => ({ ...current, key, error, loading: false }));
      } finally {
        if (interval && !controller.signal.aborted) timer = setTimeout(tick, interval);
      }
    };
    const tick = () => {
      if (document.visibilityState === 'hidden') timer = setTimeout(tick, interval);
      else void run();
    };
    void run();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [key, interval, revision]);
  const current = key && state.key === key ? state : { data: undefined, error: undefined, loading: !!key };
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  return { ...current, refresh };
}

export function formatDate(value: string | null, short = false) {
  if (!value) return t('尚未同步', 'Not synced yet');
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t('时间未知', 'Unknown time');
  return new Intl.DateTimeFormat(
    getLocale() === 'zh' ? 'zh-CN' : 'en-US',
    short
      ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
      : { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false },
  ).format(date);
}
export function remaining(expires: string, now: number) {
  const minutes = Math.ceil((Date.parse(expires) - now) / 60_000);
  if (minutes <= 0) return t('已过期', 'Expired');
  if (minutes < 60)
    return t(`剩余 ${minutes} 分钟`, `${minutes} ${minutes === 1 ? 'minute' : 'minutes'} left`);
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return t(
    `剩余 ${hours} 小时${rest ? ` ${rest} 分钟` : ''}`,
    `${hours} ${hours === 1 ? 'hour' : 'hours'}${rest ? ` ${rest} ${rest === 1 ? 'minute' : 'minutes'}` : ''} left`,
  );
}
export function providerName(id: string) {
  return { 'temp-mail-org': 'Temp Mail', 'tempmail-lol': 'TempMail.lol' }[id] ?? id;
}

export function formatDuration(seconds: number) {
  const units: [number, string, string][] = [
    [86400, '天', 'day'],
    [3600, '小时', 'hour'],
    [60, '分钟', 'minute'],
  ];
  for (const [divisor, zh, en] of units) {
    if (seconds >= divisor && seconds % divisor === 0) {
      const count = seconds / divisor;
      return t(`${count} ${zh}`, `${count} ${en}${count === 1 ? '' : 's'}`);
    }
  }
  return t(`${seconds} 秒`, `${seconds} second${seconds === 1 ? '' : 's'}`);
}
