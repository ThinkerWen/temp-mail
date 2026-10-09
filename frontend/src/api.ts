import { t } from './preferences';
import { MAILBOX_PAGE_SIZE, MESSAGE_PAGE_SIZE, OPERATION_PAGE_SIZE } from './pagination';
import type {
  CleanupPreview,
  CleanupRequest,
  Configuration,
  ConfigurationPatch,
  CreatePayload,
  DashboardData,
  Mailbox,
  Message,
  MessagePage,
  Operation,
  OperationPage,
  Page,
  Provider,
} from './types';

const errors: Record<string, [string, string]> = {
  CLEANUP_CHANGED: [
    '待清除的邮箱已变化，请核对最新数量后再次确认。',
    'The mailboxes to clear have changed. Review the updated count and confirm again.',
  ],
  CLEANUP_INVALID_CUTOFF: [
    '清理确认信息已失效，请关闭弹窗后重新统计。',
    'The cleanup confirmation is invalid. Close the dialog and count the mailboxes again.',
  ],
  TOKEN_UPDATED: [
    '配置已保存，访问令牌已立即更新。请使用新令牌重新登录。',
    'Configuration saved. Your new access token is active. Sign in with the new token.',
  ],
  UNAUTHORIZED: [
    '访问令牌无效或已变更，请重新连接。',
    'Your access token is invalid or has changed. Please reconnect.',
  ],
  PROVIDER_UNAVAILABLE: [
    '供应商当前不可用，请检查服务配置或稍后再试。',
    'The provider is unavailable. Check the configuration or try again later.',
  ],
  PROVIDER_RATE_LIMITED: [
    '供应商请求过于频繁，请稍后再试。',
    'The provider is receiving too many requests. Please try again later.',
  ],
  RATE_LIMITED: [
    '供应商请求过于频繁，请稍后再试。',
    'The provider is receiving too many requests. Please try again later.',
  ],
  MAILBOX_EXPIRED: [
    '这个邮箱已过期，已停止收发，历史邮件仍可查看。',
    'This mailbox has expired and no longer sends or receives mail. Its message history is still available.',
  ],
  MAILBOX_DELETED: ['这个邮箱已被删除。', 'This mailbox has been deleted.'],
  MAILBOX_NOT_FOUND: [
    '未找到这个邮箱，请刷新邮箱列表。',
    'Mailbox not found. Please refresh the mailbox list.',
  ],
  MESSAGE_NOT_FOUND: [
    '邮件已不可用，请刷新收件箱。',
    'This message is no longer available. Please refresh the inbox.',
  ],
  OPERATION_NOT_FOUND: ['未找到这条操作记录。', 'Operation not found.'],
  CAPABILITY_UNSUPPORTED: [
    '所选供应商不支持这项操作或有效期。',
    'The selected provider does not support this operation or lifetime.',
  ],
  TTL_UNSUPPORTED: ['所选供应商不支持这个有效期。', 'The selected provider does not support this lifetime.'],
  VALIDATION_ERROR: ['请检查填写的内容后重试。', 'Please check your entries and try again.'],
  STORAGE_UNAVAILABLE: [
    '服务暂时无法访问数据库，请稍后再试。',
    'The database is temporarily unavailable. Please try again later.',
  ],
  IDEMPOTENCY_CONFLICT: [
    '请求内容与上次提交不一致，请重新检查。',
    'This request differs from your previous submission. Please check it again.',
  ],
  WORKER_INTERRUPTED: [
    '创建过程曾被中断，结果尚不能确认。',
    'Creation was interrupted. Its outcome is still unconfirmed.',
  ],
  CONFIG_CONFLICT: [
    '配置已被其他窗口或程序修改。请重新读取配置，再合并你的修改。',
    'Another window or program changed the configuration. Reload it and reapply your changes.',
  ],
  CONFIG_INVALID: [
    '配置内容无效，请检查地址、数值和令牌后重试。',
    'Invalid configuration. Check the addresses, values, and token, then try again.',
  ],
  CONFIG_READ_FAILED: [
    '无法读取配置文件，请检查文件是否存在及读取权限。',
    'The configuration file could not be read. Check that it exists and is readable.',
  ],
  CONFIG_WRITE_FAILED: [
    '配置未能写入文件，请检查文件权限和磁盘空间后重试。',
    'The configuration could not be saved. Check file permissions and available disk space.',
  ],
  CONFIG_UNAVAILABLE: [
    '当前服务未提供可编辑的配置文件。',
    'This service does not have an editable configuration file.',
  ],
};

export function restartRequiredMessage(config: Configuration): string {
  const names = (config.restart_required_fields ?? []).map((field) =>
    field === 'app.db_path'
      ? t('数据库路径', 'Database path')
      : field === 'app.encryption_key'
        ? t('加密密钥', 'Encryption key')
        : field,
  );
  return t(
    `${names.join('、') || '部分系统设置'}的修改需要重启 API 和 Worker 后生效；其他设置已在线应用。`,
    `Restart the API and Worker to apply changes to ${names.join(', ') || 'some system settings'}. Other settings are already active.`,
  );
}

export class ApiError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    // Keep the code rather than translated copy so existing errors follow language changes.
    super(code);
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const message = errors[error.code];
    return message
      ? t(...message)
      : t(
          `请求未完成（${error.code}），请稍后重试。`,
          `Request failed (${error.code}). Please try again later.`,
        );
  }
  if (typeof error === 'string') return errors[error] ? t(...errors[error]) : error;
  return t(
    '暂时无法连接服务，请检查网络和 API 是否正常运行。',
    'Unable to connect. Check your network and make sure the API is running.',
  );
}

export async function request<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const timeout = AbortSignal.timeout(15_000);
  const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  const response = await fetch(path, {
    ...init,
    signal,
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', Authorization: `Bearer ${token}`, ...init.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(body?.error?.code ?? `HTTP_${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export const api = {
  dashboard: (token: string, signal?: AbortSignal) =>
    request<DashboardData>(token, '/v1/dashboard', { signal }),
  cleanupPreview: (token: string, signal?: AbortSignal) =>
    request<CleanupPreview>(token, '/v1/mailboxes/cleanup-preview', { signal }),
  cleanupMailboxes: (token: string, payload: CleanupRequest, signal?: AbortSignal) =>
    request<{ deleted_count: number }>(token, '/v1/mailboxes/cleanup', {
      method: 'POST',
      signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  configuration: (token: string, signal?: AbortSignal) =>
    request<Configuration>(token, '/v1/config', { signal }),
  saveConfiguration: (token: string, payload: ConfigurationPatch, signal?: AbortSignal) =>
    request<Configuration>(token, '/v1/config', {
      method: 'PUT',
      signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  providers: (token: string, signal?: AbortSignal) =>
    request<{ providers: Provider[] }>(token, '/v1/capabilities', { signal }),
  mailboxes: (token: string, offset: number, email: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(MAILBOX_PAGE_SIZE), offset: String(offset) });
    if (email) params.set('email', email);
    return request<Page<Mailbox>>(token, `/v1/mailboxes?${params}`, { signal });
  },
  messages: (token: string, mailboxId: string, offset: number, signal?: AbortSignal) =>
    request<MessagePage>(
      token,
      `/v1/mailboxes/${encodeURIComponent(mailboxId)}/messages?limit=${MESSAGE_PAGE_SIZE}&offset=${offset}`,
      { signal },
    ),
  message: (token: string, mailboxId: string, id: string, signal?: AbortSignal) =>
    request<Message>(
      token,
      `/v1/mailboxes/${encodeURIComponent(mailboxId)}/messages/${encodeURIComponent(id)}`,
      { signal },
    ),
  operations: (token: string, offset: number, signal?: AbortSignal) =>
    request<OperationPage>(token, `/v1/operations?limit=${OPERATION_PAGE_SIZE}&offset=${offset}`, { signal }),
  operation: (token: string, id: string, signal?: AbortSignal) =>
    request<Operation>(token, `/v1/operations/${encodeURIComponent(id)}`, { signal }),
  create: (token: string, payload: CreatePayload, key: string) =>
    request<Operation>(token, '/v1/mailboxes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    }),
};
