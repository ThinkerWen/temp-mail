import { Button, Chip } from '@heroui/react';
import { AlertCircle, Check, ChevronLeft, ChevronRight, Copy, Inbox, Mail, RefreshCw } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { errorMessage } from '../api';
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
  if (!error) return null;
  return (
    <div role="alert" className="notice error">
      <AlertCircle size={17} />
      <span>{typeof error === 'string' ? error : errorMessage(error)}</span>
      {retry && (
        <Button size="sm" variant="tertiary" onPress={retry}>
          重试
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
export function Loading({ label = '正在加载…' }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <RefreshCw size={18} className="spin" />
      {label}
    </div>
  );
}
export function CopyButton({ value, label = '复制邮箱地址' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setFailed(false);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setFailed(true);
    }
  }
  return (
    <span className="copy-wrap">
      <Button
        size="sm"
        variant="tertiary"
        isIconOnly
        aria-label={copied ? '已复制' : label}
        onPress={() => void copy()}
      >
        {copied ? <Check size={16} /> : <Copy size={16} />}
      </Button>
      {failed && (
        <span role="status" className="copy-error">
          请手动选择复制
        </span>
      )}
    </span>
  );
}
export function Pager({
  offset,
  count,
  loading,
  label,
  change,
}: {
  offset: number;
  count: number;
  loading: boolean;
  label: string;
  change: (value: number) => void;
}) {
  if (!offset && count < 20) return null;
  return (
    <div className="pager">
      <Button
        isIconOnly
        size="sm"
        variant="tertiary"
        aria-label={`上一页${label}`}
        isDisabled={loading || offset === 0}
        onPress={() => change(Math.max(0, offset - 20))}
      >
        <ChevronLeft size={16} />
      </Button>
      <span>第 {offset / 20 + 1} 页</span>
      <Button
        isIconOnly
        size="sm"
        variant="tertiary"
        aria-label={`下一页${label}`}
        isDisabled={loading || count < 20}
        onPress={() => change(offset + 20)}
      >
        <ChevronRight size={16} />
      </Button>
    </div>
  );
}
const statusLabels: Record<OperationStatus, string> = {
  pending: '等待处理',
  running: '创建中',
  succeeded: '已完成',
  failed: '失败',
  unknown: '结果待确认',
};
export function OperationBadge({ status }: { status: OperationStatus }) {
  return (
    <Chip size="sm" variant="soft" className={`status-chip status-${status}`}>
      <span className="status-dot" />
      {statusLabels[status]}
    </Chip>
  );
}
