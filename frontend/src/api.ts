import type { CreatePayload, Mailbox, Message, MessagePage, Operation, Page, Provider } from './types';

const errors: Record<string, string> = {
  UNAUTHORIZED: '访问令牌无效或已变更，请重新连接。',
  PROVIDER_UNAVAILABLE: '供应商当前不可用，请检查服务配置或稍后再试。',
  PROVIDER_RATE_LIMITED: '供应商请求过于频繁，请稍后再试。',
  RATE_LIMITED: '供应商请求过于频繁，请稍后再试。',
  MAILBOX_EXPIRED: '这个邮箱已过期，邮件内容不再可用。',
  MAILBOX_DELETED: '这个邮箱已被删除。',
  MAILBOX_NOT_FOUND: '未找到这个邮箱，请刷新邮箱列表。',
  MESSAGE_NOT_FOUND: '邮件已不可用，请刷新收件箱。',
  OPERATION_NOT_FOUND: '未找到这条操作记录。',
  CAPABILITY_UNSUPPORTED: '所选供应商不支持这项操作或有效期。',
  TTL_UNSUPPORTED: '所选供应商不支持这个有效期。',
  VALIDATION_ERROR: '请检查填写的内容后重试。',
  STORAGE_UNAVAILABLE: '服务暂时无法访问数据库，请稍后再试。',
  IDEMPOTENCY_CONFLICT: '请求内容与上次提交不一致，请重新检查。',
  WORKER_INTERRUPTED: '创建过程曾被中断，结果尚不能确认。',
};

export class ApiError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    super(errors[code] ?? `请求未完成（${code}），请稍后重试。`);
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return '暂时无法连接服务，请检查网络和 API 是否正常运行。';
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
  providers: (token: string, signal?: AbortSignal) =>
    request<{ providers: Provider[] }>(token, '/v1/capabilities', { signal }),
  mailboxes: (token: string, offset: number, email: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (email) params.set('email', email);
    return request<Page<Mailbox>>(token, `/v1/mailboxes?${params}`, { signal });
  },
  messages: (token: string, mailboxId: string, offset: number, signal?: AbortSignal) =>
    request<MessagePage>(
      token,
      `/v1/mailboxes/${encodeURIComponent(mailboxId)}/messages?limit=20&offset=${offset}`,
      { signal },
    ),
  message: (token: string, mailboxId: string, id: string, signal?: AbortSignal) =>
    request<Message>(
      token,
      `/v1/mailboxes/${encodeURIComponent(mailboxId)}/messages/${encodeURIComponent(id)}`,
      { signal },
    ),
  operation: (token: string, id: string, signal?: AbortSignal) =>
    request<Operation>(token, `/v1/operations/${encodeURIComponent(id)}`, { signal }),
  create: (token: string, payload: CreatePayload, key: string) =>
    request<Operation>(token, '/v1/mailboxes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    }),
};
