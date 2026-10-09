import { Button, Chip } from '@heroui/react';
import { AlertCircle, Check, ChevronLeft, ChevronRight, Copy, Inbox, Mail, RefreshCw } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { errorMessage } from '../api';
import { useI18n } from '../preferences';
import type { OperationStatus } from '../types';

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Mail size={22} strokeWidth={2.1} />
      </span>
      <span>
        temp<span className="brand-light">mail</span>
        <span className="brand-dot">.</span>
      </span>
    </div>
  );
}
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  const t = useI18n();
  if (!error) return null;
  return (
    <div role="alert" className="notice error">
      <AlertCircle size={17} />
      <span>{errorMessage(error)}</span>
      {retry && (
        <Button size="sm" variant="tertiary" onPress={retry}>
          {t('重试', 'Retry')}
        </Button>
      )}
    </div>
  );
}
export function Empty({
  icon = <Inbox size={27} strokeWidth={1.4} />,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Loading({ label }: { label?: string }) {
  const t = useI18n();
  return (
    <div className="loading-state" role="status">
      <RefreshCw size={18} className="spin" />
      {label ?? t('正在加载…', 'Loading…')}
    </div>
  );
}
export function RefreshButton({
  refresh,
  loading,
  failed,
}: {
  refresh: () => void;
  loading: boolean;
  failed: boolean;
}) {
  const t = useI18n();
  const [stage, setStage] = useState<'idle' | 'refreshing' | 'done' | 'failed'>('idle');
  const [minimumElapsed, setMinimumElapsed] = useState(false);
  useEffect(() => {
    if (stage !== 'refreshing') return;
    // Keep fast requests perceptible without delaying their data updates.
    const timer = setTimeout(() => setMinimumElapsed(true), 650);
    return () => clearTimeout(timer);
  }, [stage]);
  useEffect(() => {
    if (stage === 'refreshing' && minimumElapsed && !loading) {
      setStage(failed ? 'failed' : 'done');
    }
  }, [stage, minimumElapsed, loading, failed]);
  useEffect(() => {
    if (stage !== 'done' && stage !== 'failed') return;
    const timer = setTimeout(() => setStage('idle'), 1600);
    return () => clearTimeout(timer);
  }, [stage]);
  return (
    <Button
      size="sm"
      variant="secondary"
      className="refresh-button"
      data-state={stage}
      aria-label={t('刷新邮箱', 'Refresh mailboxes')}
      isPending={stage === 'refreshing'}
      onPress={() => {
        setMinimumElapsed(false);
        setStage('refreshing');
        refresh();
      }}
    >
      <span className="refresh-icon" aria-hidden="true">
        {stage === 'done' ? (
          <Check size={14} className="feedback-check" />
        ) : stage === 'failed' ? (
          <AlertCircle size={14} className="feedback-error" />
        ) : (
          <RefreshCw size={14} className={stage === 'refreshing' ? 'spin' : undefined} />
        )}
      </span>
      <span className="refresh-label" aria-live="polite" aria-atomic="true">
        {
          {
            idle: t('刷新', 'Refresh'),
            refreshing: t('刷新中', 'Refreshing'),
            done: t('已刷新', 'Refreshed'),
            failed: t('刷新失败', 'Refresh failed'),
          }[stage]
        }
      </span>
    </Button>
  );
}
export function CopyButton({ value, label }: { value: string; label?: string }) {
  const t = useI18n();
  const copyLabel = label ?? t('复制邮箱地址', 'Copy email address');
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const request = useRef(0);
  useEffect(() => {
    setCopied(false);
    setFailed(false);
    return () => {
      request.current += 1;
      clearTimeout(timer.current);
    };
  }, [value]);
  async function copy() {
    const current = ++request.current;
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      if (current !== request.current) return;
      setCopied(true);
      setFailed(false);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      if (current !== request.current) return;
      setCopied(false);
      setFailed(true);
    }
  }
  return (
    <span className="copy-wrap" title={copied ? t('已复制', 'Copied') : copyLabel}>
      <Button
        size="sm"
        variant="tertiary"
        isIconOnly
        className="copy-button"
        data-copied={copied}
        aria-label={copied ? t('已复制', 'Copied') : copyLabel}
        onPress={() => void copy()}
      >
        {copied ? <Check size={16} className="feedback-check" /> : <Copy size={16} />}
      </Button>
      {copied && (
        <span role="status" className="copy-feedback">
          {t('已复制', 'Copied')}
        </span>
      )}
      {failed && (
        <span role="status" className="copy-error">
          {t('请手动选择复制', 'Please select and copy manually')}
        </span>
      )}
    </span>
  );
}
export function Pager({
  offset,
  pageSize,
  count,
  total,
  loading,
  label,
  change,
}: {
  offset: number;
  pageSize: number;
  count: number;
  total?: number;
  loading: boolean;
  label: string;
  change: (value: number) => void;
}) {
  const t = useI18n();
  if (!offset && (total === undefined ? count < pageSize : total <= pageSize)) return null;
  return (
    <div className="pager">
      <Button
        isIconOnly
        size="sm"
        variant="tertiary"
        aria-label={t(`上一页${label}`, `Previous page of ${label}`)}
        isDisabled={loading || offset === 0}
        onPress={() => change(Math.max(0, offset - pageSize))}
      >
        <ChevronLeft size={16} />
      </Button>
      <span>{t(`第 ${offset / pageSize + 1} 页`, `Page ${offset / pageSize + 1}`)}</span>
      <Button
        isIconOnly
        size="sm"
        variant="tertiary"
        aria-label={t(`下一页${label}`, `Next page of ${label}`)}
        isDisabled={loading || (total === undefined ? count < pageSize : offset + count >= total)}
        onPress={() => change(offset + pageSize)}
      >
        <ChevronRight size={16} />
      </Button>
    </div>
  );
}
const statusLabels: Record<OperationStatus, [string, string]> = {
  pending: ['等待处理', 'Pending'],
  running: ['创建中', 'Creating'],
  succeeded: ['已完成', 'Completed'],
  failed: ['失败', 'Failed'],
  unknown: ['结果待确认', 'Unconfirmed'],
};
export function OperationBadge({ status }: { status: OperationStatus }) {
  const t = useI18n();
  return (
    <Chip size="sm" variant="soft" className={`status-chip status-${status}`}>
      <span className="status-dot" />
      {t(...statusLabels[status])}
    </Chip>
  );
}
