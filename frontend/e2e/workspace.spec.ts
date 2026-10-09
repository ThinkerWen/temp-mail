import { expect, test, type Page, type Request } from '@playwright/test';
import type { Capabilities, Mailbox, Message, Operation, Provider } from '../src/types';

const token = 'e2e-only-token-not-a-real-credential';
const capabilities: Capabilities = {
  receive: true,
  send: false,
  delete: false,
  attachments: false,
  webhook: false,
  custom_local_part: false,
  max_ttl_seconds: 3600,
};
const providers: Provider[] = [
  { id: 'temp-mail-org', capabilities },
  { id: 'tempmail-lol', capabilities: { ...capabilities, max_ttl_seconds: 600 } },
];

function configuration() {
  return {
    revision: 'initial-config-revision',
    restart_required: false,
    restart_required_fields: [] as string[],
    app: { db_path: './data/temp-mail.db', api_token_configured: true, encryption_key_configured: true },
    worker: {
      sync_interval_seconds: 15,
      operation_timeout_seconds: 300,
      poll_seconds: 1,
      create_concurrency: 2,
      receive_concurrency: 4,
    },
    providers: providers.map((provider, index) => ({
      id: provider.id,
      enabled: index === 0,
      index_url: index === 0 ? 'https://temp-mail.org/' : 'https://tempmail.lol/',
      base_url: index === 0 ? 'https://web2.temp-mail.org' : 'https://api.tempmail.lol/v2',
      timeout_seconds: 15,
      max_ttl_seconds: provider.capabilities.max_ttl_seconds,
      capabilities: { ...provider.capabilities },
      impersonate: 'chrome110',
      proxy_configured: index === 0,
    })),
  };
}

function mailbox(overrides: Partial<Mailbox> = {}): Mailbox {
  return {
    id: 'mailbox-1',
    email: 'inbox@example.test',
    provider_id: 'temp-mail-org',
    capabilities,
    status: 'active',
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 3600_000).toISOString(),
    last_synced_at: new Date().toISOString(),
    last_sync_error_code: null,
    ...overrides,
  };
}

function operation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: 'operation-1',
    kind: 'create',
    status: 'pending',
    provider_id: 'temp-mail-org',
    mailbox_id: null,
    result: null,
    error_code: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

const message: Message = {
  id: 'message-1',
  mailbox_id: 'mailbox-1',
  sender: 'sender@example.test',
  recipients: ['inbox@example.test'],
  subject: '欢迎使用临时邮箱',
  received_at: new Date().toISOString(),
  text: '<img src=x onerror="window.__mailExecuted=true">\n<script>window.__mailExecuted=true</script>\n这是可安全阅读的正文。',
};

interface MockState {
  activeToken: string;
  configResponseGate?: Promise<void>;
  mailboxes: Mailbox[];
  operation: Operation;
  operations: Operation[];
  operationQueries: URL[];
  posts: Request[];
  queries: URL[];
  detailError?: string;
  abortNextPost?: boolean;
  completeOperation?: boolean;
  config: ReturnType<typeof configuration>;
  configWrites: Request[];
  configFailure?: string;
  cleanupPreviews: { count: number; cutoff: string; revision: string }[];
  cleanupPreviewQueries: URL[];
  cleanupPosts: Request[];
  cleanupPreviewFailure?: string;
  cleanupFailure?: string;
  cleanupResponseGate?: Promise<void>;
}

async function mockApi(page: Page): Promise<MockState> {
  const state: MockState = {
    activeToken: token,
    mailboxes: [mailbox()],
    operation: operation(),
    operations: [],
    operationQueries: [],
    posts: [],
    queries: [],
    config: configuration(),
    configWrites: [],
    cleanupPreviews: [],
    cleanupPreviewQueries: [],
    cleanupPosts: [],
  };
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.headers().authorization !== `Bearer ${state.activeToken}`) {
      await route.fulfill({ status: 401, json: { error: { code: 'UNAUTHORIZED' } } });
      return;
    }
    if (url.pathname === '/v1/config') {
      if (request.method() === 'PUT') {
        state.configWrites.push(request);
        const body = request.postDataJSON();
        const code =
          state.configFailure ?? (body.revision !== state.config.revision ? 'CONFIG_CONFLICT' : null);
        if (code) {
          await route.fulfill({ status: code === 'CONFIG_CONFLICT' ? 409 : 503, json: { error: { code } } });
          return;
        }
        if (body.app) {
          state.config.app.db_path = body.app.db_path ?? state.config.app.db_path;
          state.activeToken = body.app.api_token ?? state.activeToken;
        }
        if (body.worker) state.config.worker = { ...state.config.worker, ...body.worker };
        if (body.providers) {
          state.config.providers = body.providers.map(
            (provider: ReturnType<typeof configuration>['providers'][number] & { proxy?: string | null }) => {
              const { proxy, ...fields } = provider;
              const previous = state.config.providers.find((item) => item.id === provider.id)!;
              return {
                ...fields,
                capabilities: { ...previous.capabilities, max_ttl_seconds: fields.max_ttl_seconds },
                proxy_configured:
                  proxy === null
                    ? false
                    : !!proxy ||
                      !!state.config.providers.find((item) => item.id === provider.id)?.proxy_configured,
              };
            },
          );
        }
        state.config.revision = `config-revision-${state.configWrites.length}`;
        state.config.restart_required_fields =
          state.config.app.db_path === './data/temp-mail.db' ? [] : ['app.db_path'];
        state.config.restart_required = state.config.restart_required_fields.length > 0;
        if (state.configResponseGate) await state.configResponseGate;
      }
      await route.fulfill({ json: state.config });
    } else if (url.pathname === '/v1/capabilities') {
      await route.fulfill({
        json: {
          providers: state.config.providers
            .filter((provider) => provider.enabled)
            .map(({ id, capabilities }) => ({ id, capabilities })),
        },
      });
    } else if (url.pathname === '/v1/mailboxes/cleanup-preview') {
      state.cleanupPreviewQueries.push(url);
      if (state.cleanupPreviewFailure) {
        await route.fulfill({
          status: state.cleanupPreviewFailure === 'UNAUTHORIZED' ? 401 : 503,
          json: { error: { code: state.cleanupPreviewFailure } },
        });
        return;
      }
      const cutoff = new Date().toISOString();
      const eligible = state.mailboxes.filter(
        (item) =>
          Date.parse(item.created_at) <= Date.parse(cutoff) &&
          (item.status === 'deleted' || Date.parse(item.expires_at) <= Date.parse(cutoff)),
      );
      const preview = {
        count: eligible.length,
        cutoff,
        revision:
          eligible
            .map((item) => item.id)
            .sort()
            .join(':') || 'empty-cleanup-revision',
      };
      state.cleanupPreviews.push(preview);
      await route.fulfill({ json: preview });
    } else if (url.pathname === '/v1/mailboxes/cleanup' && request.method() === 'POST') {
      state.cleanupPosts.push(request);
      if (state.cleanupResponseGate) await state.cleanupResponseGate;
      const body = request.postDataJSON();
      const eligible = state.mailboxes.filter(
        (item) =>
          Date.parse(item.created_at) <= Date.parse(body.cutoff) &&
          (item.status === 'deleted' || Date.parse(item.expires_at) <= Date.parse(body.cutoff)),
      );
      const revision =
        eligible
          .map((item) => item.id)
          .sort()
          .join(':') || 'empty-cleanup-revision';
      const code =
        state.cleanupFailure ??
        (body.expected_count !== eligible.length || body.revision !== revision ? 'CLEANUP_CHANGED' : null);
      if (code) {
        await route.fulfill({
          status: code === 'CLEANUP_CHANGED' ? 409 : code === 'UNAUTHORIZED' ? 401 : 503,
          json: { error: { code } },
        });
        return;
      }
      const deleted = new Set(eligible.map((item) => item.id));
      state.mailboxes = state.mailboxes.filter((item) => !deleted.has(item.id));
      state.operations = state.operations.map((item) =>
        item.mailbox_id && deleted.has(item.mailbox_id) ? { ...item, mailbox_id: null } : item,
      );
      await route.fulfill({ json: { deleted_count: eligible.length } });
    } else if (url.pathname === '/v1/mailboxes' && request.method() === 'POST') {
      state.posts.push(request);
      if (state.abortNextPost) {
        state.abortNextPost = false;
        await route.abort('failed');
        return;
      }
      if (!state.operations.some((item) => item.id === state.operation.id)) {
        state.operations.unshift(state.operation);
      }
      await route.fulfill({ status: 202, json: state.operation });
    } else if (url.pathname === '/v1/mailboxes') {
      state.queries.push(url);
      const email = url.searchParams.get('email');
      const offset = Number(url.searchParams.get('offset') ?? 0);
      const limit = Number(url.searchParams.get('limit') ?? 20);
      const matching = state.mailboxes.filter((item) => !email || item.email === email);
      const items = matching.slice(offset, offset + limit);
      await route.fulfill({ json: { items, limit, offset, total: matching.length } });
    } else if (url.pathname === '/v1/operations' || url.pathname.startsWith('/v1/operations/')) {
      if (state.completeOperation && state.operations.some((item) => item.id === state.operation.id)) {
        state.operation = operation({
          id: state.operation.id,
          status: 'succeeded',
          mailbox_id: 'mailbox-created',
          result: { mailbox_id: 'mailbox-created', email: 'created@example.test' },
        });
        if (!state.mailboxes.some((item) => item.id === 'mailbox-created')) {
          state.mailboxes.unshift(mailbox({ id: 'mailbox-created', email: 'created@example.test' }));
        }
        state.operations = state.operations.map((item) =>
          item.id === state.operation.id ? state.operation : item,
        );
      }
      if (url.pathname === '/v1/operations') {
        state.operationQueries.push(url);
        const limit = Number(url.searchParams.get('limit') ?? 20);
        const offset = Number(url.searchParams.get('offset') ?? 0);
        await route.fulfill({
          json: {
            items: state.operations.slice(offset, offset + limit),
            limit,
            offset,
            total: state.operations.length,
          },
        });
      } else {
        const id = decodeURIComponent(url.pathname.slice('/v1/operations/'.length));
        const item =
          state.operations.find((item) => item.id === id) ??
          (id === state.operation.id ? state.operation : undefined);
        await route.fulfill(
          item ? { json: item } : { status: 404, json: { error: { code: 'OPERATION_NOT_FOUND' } } },
        );
      }
    } else if (/\/messages\/message-1$/.test(url.pathname)) {
      const selected = state.mailboxes.find((item) => url.pathname.includes(`/${item.id}/`));
      if (!selected) {
        await route.fulfill({ status: 404, json: { error: { code: 'MAILBOX_NOT_FOUND' } } });
      } else if (selected.status === 'deleted') {
        await route.fulfill({ status: 410, json: { error: { code: 'MAILBOX_DELETED' } } });
      } else if (state.detailError) {
        await route.fulfill({ status: 410, json: { error: { code: state.detailError } } });
      } else if (selected.id !== message.mailbox_id) {
        await route.fulfill({ status: 404, json: { error: { code: 'MESSAGE_NOT_FOUND' } } });
      } else await route.fulfill({ json: message });
    } else if (/\/messages$/.test(url.pathname)) {
      const selected = state.mailboxes.find((item) => url.pathname.includes(`/${item.id}/`));
      if (!selected) {
        await route.fulfill({ status: 404, json: { error: { code: 'MAILBOX_NOT_FOUND' } } });
      } else if (selected.status === 'deleted') {
        await route.fulfill({ status: 410, json: { error: { code: 'MAILBOX_DELETED' } } });
      } else {
        const { text: _text, ...summary } = message;
        await route.fulfill({
          json: {
            items: selected?.id === 'mailbox-1' ? [summary] : [],
            limit: 20,
            offset: 0,
            last_synced_at: new Date().toISOString(),
          },
        });
      }
    } else {
      throw new Error(`Unexpected mocked API request: ${request.method()} ${url.pathname}`);
    }
  });
  return state;
}

async function connect(page: Page, path = '/') {
  await page.goto(path);
  await page.getByLabel('访问令牌', { exact: true }).fill(token);
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible();
}

function expiredMailboxes(count: number): Mailbox[] {
  return Array.from({ length: count }, (_, index) =>
    mailbox({
      id: `expired-${index + 1}`,
      email: `expired-${index + 1}@example.test`,
      status: 'expired',
      expires_at: new Date(Date.now() - 60_000).toISOString(),
    }),
  );
}

test('previews all expired mailboxes independently of pagination and search and cancels without deleting', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes = [
    ...Array.from({ length: 7 }, (_, index) =>
      mailbox({ id: `active-${index + 1}`, email: `active-${index + 1}@example.test` }),
    ),
    ...expiredMailboxes(4),
    mailbox({ id: 'deleted-local', email: 'deleted@example.test', status: 'deleted' }),
    mailbox({
      id: 'elapsed-active',
      email: 'elapsed@example.test',
      expires_at: new Date(Date.now() - 60_000).toISOString(),
    }),
  ];
  await connect(page);
  await page.getByRole('button', { name: '下一页邮箱', exact: true }).click();
  await expect(page.getByRole('button', { name: 'active-6@example.test', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByText('将清除 6 个失效邮箱。', { exact: true })).toBeVisible();
  await expect(dialog).toContainText('不受当前搜索和分页影响');
  expect(state.cleanupPosts).toHaveLength(0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.mailboxes).toHaveLength(13);

  await page.getByLabel('搜索邮箱', { exact: true }).fill('active-1@example.test');
  await page.getByLabel('搜索邮箱', { exact: true }).press('Enter');
  await expect(page.locator('.mailbox-row')).toHaveCount(1);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '确认清除 6 个邮箱', exact: true })).toBeEnabled();
  expect(state.cleanupPreviewQueries.every((url) => !url.search)).toBe(true);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(state.cleanupPosts).toHaveLength(0);
  expect(state.mailboxes).toHaveLength(13);
});

test('clears expired mailboxes after confirmation, returns to page one, and retains operation history without stale mailbox links', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes = [
    ...Array.from({ length: 7 }, (_, index) =>
      mailbox({ id: `active-${index + 1}`, email: `active-${index + 1}@example.test` }),
    ),
    ...expiredMailboxes(6),
  ];
  state.operations = [
    operation({
      id: 'op_expired_history',
      status: 'succeeded',
      mailbox_id: 'expired-1',
      result: { mailbox_id: 'expired-1', email: 'expired-1@example.test' },
    }),
  ];
  await connect(page);
  await page.getByRole('button', { name: '下一页邮箱', exact: true }).click();
  await expect(page.getByRole('button', { name: 'expired-1@example.test', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'expired-1@example.test', exact: true }).click();
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByRole('button', { name: '确认清除 6 个邮箱', exact: true })).toBeEnabled();
  const preview = state.cleanupPreviews.at(-1)!;
  expect(state.cleanupPosts).toHaveLength(0);
  await dialog.getByRole('button', { name: '确认清除 6 个邮箱', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '已清除 6 个失效邮箱。' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'active-1@example.test', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '上一页邮箱', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'expired-1@example.test', exact: true })).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(1);
  expect(state.cleanupPosts[0].postDataJSON()).toEqual({
    cutoff: preview.cutoff,
    expected_count: 6,
    revision: preview.revision,
  });
  expect(state.mailboxes).toHaveLength(7);
  await page.getByRole('link', { name: /^操作记录/ }).click();
  const row = page.locator('.operation-row').filter({ hasText: 'op_expired_history' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('expired-1@example.test');
  await expect(row.getByRole('button', { name: '查看邮箱', exact: true })).toHaveCount(0);
  expect(state.operations[0].mailbox_id).toBeNull();
  expect(state.operations[0].result?.mailbox_id).toBe('expired-1');
  await page.reload();
  await expect(row).toBeVisible();
  await expect(row.getByRole('button', { name: '查看邮箱', exact: true })).toHaveCount(0);
});

test('disables cleanup confirmation when no expired mailboxes exist', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByText('当前没有可清除的失效邮箱。', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认清除 0 个邮箱', exact: true })).toBeDisabled();
  expect(state.cleanupPosts).toHaveLength(0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.mailboxes).toHaveLength(1);
});

test('retries failed cleanup previews without sending a deletion request', async ({ page }) => {
  const state = await mockApi(page);
  state.mailboxes.push(...expiredMailboxes(1));
  state.cleanupPreviewFailure = 'STORAGE_UNAVAILABLE';
  await connect(page);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByRole('alert')).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认清除', exact: true })).toBeDisabled();
  expect(state.cleanupPosts).toHaveLength(0);
  state.cleanupPreviewFailure = undefined;
  await dialog.getByRole('button', { name: '重试', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true })).toBeEnabled();
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(0);
});

test('keeps the cleanup dialog and its preview after a failed deletion and permits an explicit retry', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes.push(...expiredMailboxes(1));
  state.cleanupFailure = 'STORAGE_UNAVAILABLE';
  await connect(page);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true })).toBeEnabled();
  expect(state.mailboxes).toHaveLength(2);
  expect(state.cleanupPosts).toHaveLength(1);
  state.cleanupFailure = undefined;
  await dialog.getByRole('button', { name: '重试', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '已清除 1 个失效邮箱。' })).toBeVisible();
  expect(state.cleanupPosts).toHaveLength(2);
  expect(state.cleanupPosts[1].postDataJSON()).toEqual(state.cleanupPosts[0].postDataJSON());
  expect(state.mailboxes).toHaveLength(1);
});

test('refreshes a changed cleanup preview and requires a second confirmation before deleting', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes.push(...expiredMailboxes(1));
  await connect(page);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true })).toBeEnabled();
  state.mailboxes.push(
    mailbox({
      id: 'expired-later',
      email: 'later@example.test',
      created_at: new Date(Date.now() - 120_000).toISOString(),
      status: 'expired',
      expires_at: new Date(Date.now() - 60_000).toISOString(),
    }),
  );
  await dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true }).click();
  await expect(dialog).toContainText('待清除的邮箱已变化。请核对最新统计后再次确认。');
  await expect(dialog.getByRole('button', { name: '确认清除 2 个邮箱', exact: true })).toBeEnabled();
  expect(state.cleanupPosts).toHaveLength(1);
  expect(state.mailboxes).toHaveLength(3);
  expect(state.cleanupPreviews).toHaveLength(2);
  const revised = state.cleanupPreviews.at(-1)!;
  await dialog.getByRole('button', { name: '确认清除 2 个邮箱', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(2);
  expect(state.cleanupPosts[1].postDataJSON()).toEqual({
    cutoff: revised.cutoff,
    expected_count: 2,
    revision: revised.revision,
  });
  expect(state.mailboxes).toHaveLength(1);
});

for (const endpoint of ['preview', 'confirmation'] as const) {
  test(`disconnects when cleanup ${endpoint} rejects authorization`, async ({ page }) => {
    const state = await mockApi(page);
    state.mailboxes.push(...expiredMailboxes(1));
    if (endpoint === 'preview') state.cleanupPreviewFailure = 'UNAUTHORIZED';
    else state.cleanupFailure = 'UNAUTHORIZED';
    await connect(page);
    await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
    if (endpoint === 'confirmation') {
      await page.getByRole('dialog').getByRole('button', { name: '确认清除 1 个邮箱', exact: true }).click();
    }
    await expect(page.getByRole('button', { name: '连接工作台', exact: true })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('alert')).toContainText('访问令牌无效或已变更');
    expect(state.mailboxes).toHaveLength(2);
  });
}

test('blocks repeat cleanup submissions and dismissal until the confirmed request finishes', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes.push(...expiredMailboxes(1));
  let release!: () => void;
  state.cleanupResponseGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await connect(page);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  try {
    await dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true }).click();
    await expect.poll(() => state.cleanupPosts.length).toBe(1);
    const pending = dialog.getByRole('button', { name: '正在清除…', exact: true });
    await expect(pending).toBeDisabled();
    await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
    await expect(dialog.getByRole('button', { name: '关闭清除窗口', exact: true })).toHaveCount(0);
    await pending.evaluate((button) => {
      (button as HTMLButtonElement).click();
      (button as HTMLButtonElement).click();
    });
    await page.keyboard.press('Escape');
    await expect(dialog).toBeVisible();
    expect(state.cleanupPosts).toHaveLength(1);
  } finally {
    release();
  }
  await expect(dialog).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(1);
});

for (const locale of ['zh', 'en'] as const) {
  test(`keeps the cleanup confirmation usable without horizontal overflow on mobile in ${locale}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await mockApi(page);
    state.mailboxes.push(...expiredMailboxes(12));
    await connect(page);
    if (locale === 'en') {
      await page.getByRole('button', { name: /界面语言/ }).click();
      await page.getByRole('option', { name: 'English', exact: true }).click();
    }
    const label = locale === 'zh' ? '清除失效邮箱' : 'Clear expired mailboxes';
    await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByRole('button', { name: label, exact: true }).click();
    const dialog = page.getByRole('dialog', { name: label, exact: true });
    const confirmation = dialog.getByRole('button', {
      name: locale === 'zh' ? '确认清除 12 个邮箱' : 'Clear 12 mailboxes',
      exact: true,
    });
    await expect(confirmation).toBeEnabled();
    await expect(dialog).toContainText(locale === 'zh' ? '操作记录会保留' : 'Operation history is retained');
    const bounds = await dialog.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(391);
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`mobile-${locale}-cleanup.png`), fullPage: true });
    await dialog.getByRole('button', { name: locale === 'zh' ? '取消' : 'Cancel', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(state.cleanupPosts).toHaveLength(0);
  });
}

for (const width of [1440, 390]) {
  test(`opens and closes the create dialog without shifting the workspace at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockApi(page);
    await connect(page);
    await expect(page.getByRole('button', { name: 'inbox@example.test', exact: true })).toBeVisible();
    // Expose first-open layout shifts even when the local module normally loads in a single frame.
    await page.route('**/components/CreateMailbox.tsx', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.continue();
    });
    const trigger = page.getByRole('button', { name: '新建邮箱', exact: true });
    await trigger.scrollIntoViewIfNeeded();
    const shell = page.locator('.main-shell');
    const original = await shell.boundingBox();
    const widths = page.evaluate(
      () =>
        new Promise<number[]>((resolve) => {
          const samples: number[] = [];
          const start = performance.now();
          function sample() {
            samples.push(document.querySelector('.main-shell')!.getBoundingClientRect().width);
            if (performance.now() - start < 900) requestAnimationFrame(sample);
            else resolve(samples);
          }
          sample();
        }),
    );
    await trigger.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    const samples = await widths;
    expect(Math.max(...samples) - Math.min(...samples)).toBeLessThan(1);
    expect(await shell.boundingBox()).toEqual(original);
    const exitedSmoothly = page.evaluate(
      () =>
        new Promise<boolean>((resolve) => {
          const overlay = document.querySelector('[data-slot="modal-backdrop"]')!;
          const observer = new MutationObserver(() => {
            if (overlay.getAttribute('data-exiting') === 'true') finish(true);
          });
          const timeout = setTimeout(() => finish(false), 2000);
          function finish(value: boolean) {
            observer.disconnect();
            clearTimeout(timeout);
            resolve(value);
          }
          observer.observe(overlay, { attributes: true, attributeFilter: ['data-exiting'] });
        }),
    );
    await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(await exitedSmoothly).toBe(true);
    await expect(trigger).toBeFocused();
    expect(await shell.boundingBox()).toEqual(original);
    await trigger.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
}

test('persists dark mode and English across reload and logout', async ({ page }, testInfo) => {
  const state = await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: '切换到深色模式', exact: true }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.getByRole('button', { name: /界面语言/ }).click();
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('button', { name: 'New mailbox', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Log out', exact: true })).toBeVisible();
  expect(state.configWrites).toHaveLength(0);
  await page.reload();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await expect(page.getByRole('button', { name: /Interface language/ })).toContainText('English');
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByText(message.text, { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath('dark-english-inbox.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.getByRole('button', { name: 'Log out', exact: true }).click();
  await expect(page.getByLabel('Access token', { exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveClass(/dark/);
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(token);
  await page.screenshot({
    path: testInfo.outputPath('dark-english-login.png'),
    fullPage: true,
    animations: 'disabled',
  });
});

test('changes language without losing unsaved system configuration', async ({ page }, testInfo) => {
  await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/unsaved.db');
  await page.getByRole('button', { name: '切换到深色模式', exact: true }).click();
  await page.getByRole('button', { name: /界面语言/ }).click();
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.getByLabel('Database path', { exact: true })).toHaveValue('./data/unsaved.db');
  await expect(page.getByRole('button', { name: 'Save configuration', exact: true })).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath('dark-english-settings.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.getByRole('button', { name: /Interface language/ }).click();
  await page.getByRole('option', { name: '中文', exact: true }).click();
  await expect(page.getByLabel('数据库路径', { exact: true })).toHaveValue('./data/unsaved.db');
  await page.getByRole('button', { name: '切换到浅色模式', exact: true }).click();
  await expect(page.locator('html')).toHaveClass(/light/);
});

test('supports English provider dialogs and header controls on mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await connect(page, '/providers');
  await page.getByRole('button', { name: '切换到深色模式', exact: true }).click();
  await page.getByRole('button', { name: /界面语言/ }).click();
  await expect(page.getByRole('option', { name: 'English', exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath('dark-mobile-language-dropdown.png'),
    animations: 'disabled',
  });
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Log out', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath('dark-english-mobile-providers.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.getByRole('button', { name: 'Edit TempMail.lol', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit TempMail.lol', exact: true });
  await expect(dialog.getByLabel('Maximum local lifetime (seconds)', { exact: true })).toHaveValue('600');
  await expect(dialog.getByLabel('API URL', { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath('dark-english-mobile-dialog.png'),
    animations: 'disabled',
  });
});

test('keeps preference controls usable when browser storage is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {
      get() {
        throw new DOMException('Storage blocked', 'SecurityError');
      },
    });
  });
  await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: '切换到深色模式', exact: true }).click();
  await page.getByRole('button', { name: /界面语言/ }).click();
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await expect(page.getByRole('button', { name: 'New mailbox', exact: true })).toBeVisible();
});

test('synchronizes preference changes across tabs without clearing drafts', async ({ page, context }) => {
  await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/cross-tab-draft.db');
  const second = await context.newPage();
  await mockApi(second);
  await connect(second);
  await second.getByRole('button', { name: '切换到深色模式', exact: true }).click();
  await second.getByRole('button', { name: /界面语言/ }).click();
  await second.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await expect(page.getByLabel('Database path', { exact: true })).toHaveValue('./data/cross-tab-draft.db');
  await second.close();
});

test('saves system configuration and keeps saved values after reload', async ({ page }, testInfo) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  const save = page.getByRole('button', { name: '保存配置', exact: true });
  await expect(save).toBeDisabled();
  await page.getByLabel('邮件同步间隔（秒）', { exact: true }).fill('45');
  await page.getByLabel('队列检查间隔（秒）', { exact: true }).fill('0.5');
  await expect(page.getByLabel('创建邮箱并发', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('收取邮件并发', { exact: true })).toHaveValue('4');
  await page.getByLabel('创建邮箱并发', { exact: true }).fill('3');
  await page.getByLabel('收取邮件并发', { exact: true }).fill('8');
  await save.click();
  await expect(page.getByText('配置已保存到 config.yaml，在线设置已生效。', { exact: true })).toBeVisible();
  expect(state.configWrites[0].postDataJSON()).toEqual({
    revision: 'initial-config-revision',
    app: { db_path: './data/temp-mail.db' },
    worker: {
      sync_interval_seconds: 45,
      operation_timeout_seconds: 300,
      poll_seconds: 0.5,
      create_concurrency: 3,
      receive_concurrency: 8,
    },
  });
  await expect(page.getByLabel('新的访问令牌', { exact: true })).toHaveValue('');
  await page.reload();
  await expect(page.getByLabel('邮件同步间隔（秒）', { exact: true })).toHaveValue('45');
  await expect(page.getByLabel('创建邮箱并发', { exact: true })).toHaveValue('3');
  await expect(page.getByLabel('收取邮件并发', { exact: true })).toHaveValue('8');
  expect(state.config.restart_required).toBe(false);
  await expect(page.locator('.config-restart')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop-settings.png'), fullPage: true });
});

test('validates independent concurrency limits and keeps their drafts across language changes', async ({
  page,
}) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  const create = page.getByLabel('创建邮箱并发', { exact: true });
  const receive = page.getByLabel('收取邮件并发', { exact: true });
  for (const field of [create, receive]) {
    for (const value of ['0', '33', '1.5']) {
      await field.fill(value);
      await page.getByRole('button', { name: '保存配置', exact: true }).click();
      expect(state.configWrites).toHaveLength(0);
      expect(await field.evaluate((input: HTMLInputElement) => input.validity.valid)).toBe(false);
    }
    await field.fill('1');
  }
  await receive.fill('32');
  await page.getByRole('button', { name: /界面语言/ }).click();
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.getByLabel('Mailbox creation concurrency', { exact: true })).toHaveValue('1');
  await expect(page.getByLabel('Mail receiving concurrency', { exact: true })).toHaveValue('32');
  await expect(page.getByText(/Creation and receiving run independently/)).toBeVisible();
  await page.getByRole('button', { name: 'Save configuration', exact: true }).click();
  await expect(
    page.getByText('Configuration saved to config.yaml. Online settings are active.', { exact: true }),
  ).toBeVisible();
  expect(state.configWrites).toHaveLength(1);
  expect(state.config.worker.create_concurrency).toBe(1);
  expect(state.config.worker.receive_concurrency).toBe(32);
});

test('shows supported provider cards and saves enable switches immediately', async ({ page }, testInfo) => {
  const state = await mockApi(page);
  await connect(page, '/providers');
  await expect(page.getByRole('switch', { name: '启用 TempMail.lol', exact: true })).not.toBeChecked();
  await expect(page.getByRole('switch', { name: '启用 Temp Mail', exact: true })).toBeChecked();
  await expect(page.getByLabel('API 地址', { exact: true })).toHaveCount(0);
  await expect(page.getByText('temp-mail-org', { exact: true })).toHaveCount(1);
  await expect(page.getByText('tempmail-lol', { exact: true })).toHaveCount(1);
  await page
    .locator('.provider-enable')
    .filter({ has: page.getByRole('switch', { name: '启用 TempMail.lol', exact: true }) })
    .click();
  await expect(page.getByRole('switch', { name: '启用 TempMail.lol', exact: true })).toBeChecked();
  expect(state.configWrites).toHaveLength(1);
  const body = state.configWrites[0].postDataJSON();
  expect(Object.keys(body).sort()).toEqual(['providers', 'revision']);
  expect(body.providers[1].enabled).toBe(true);
  for (const provider of body.providers) {
    expect(provider).not.toHaveProperty('proxy');
    expect(provider).not.toHaveProperty('capabilities');
    expect(provider).not.toHaveProperty('proxy_configured');
  }
  await expect(page.getByRole('link', { name: /^供应商/ }).locator('.nav-count')).toHaveText('2');
  await page.getByRole('link', { name: '邮箱工作台', exact: true }).click();
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByLabel('邮箱供应商', { exact: true }).click();
  await expect(page.getByRole('option', { name: 'TempMail.lol', exact: true })).toBeVisible();
  await page.getByRole('option', { name: 'TempMail.lol', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: '取消', exact: true })
    .filter({ hasText: '取消' })
    .click();
  await page.getByRole('link', { name: /^供应商/ }).click();
  await page.reload();
  await expect(page.getByRole('switch', { name: '启用 TempMail.lol', exact: true })).toBeChecked();
  await page.screenshot({
    path: testInfo.outputPath('desktop-provider-cards.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByRole('switch', { name: '启用 TempMail.lol', exact: true })).toBeChecked();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath('mobile-provider-cards.png'),
    fullPage: true,
    animations: 'disabled',
  });
});

test('applies provider lifetime changes to new mailboxes without reloading', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/providers');
  await page.getByRole('button', { name: '编辑 Temp Mail', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 Temp Mail', exact: true });
  await dialog.getByLabel('最长本地有效期（秒）', { exact: true }).fill('172800');
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.config-restart')).toHaveCount(0);
  await page.getByRole('link', { name: '邮箱工作台', exact: true }).click();
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByLabel('有效期', { exact: true }).click();
  await page.getByRole('option', { name: '2 天（最长）', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  expect(state.posts[0].postDataJSON().ttl_seconds).toBe(172800);
});

test('identifies only the database path as requiring restart after a mixed settings save', async ({
  page,
}) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/new.db');
  await page.getByLabel('邮件同步间隔（秒）', { exact: true }).fill('45');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.locator('.config-restart')).toHaveText(
    '数据库路径的修改需要重启 API 和 Worker 后生效；其他设置已在线应用。',
  );
  expect(state.config.restart_required_fields).toEqual(['app.db_path']);
  expect(state.config.worker.sync_interval_seconds).toBe(45);
  await page.reload();
  await expect(page.getByLabel('数据库路径', { exact: true })).toHaveValue('./data/new.db');
  await expect(page.locator('.config-restart')).toBeVisible();
});

test('changes the access token immediately and preserves the successful notice across polling races', async ({
  page,
}) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  const newToken = 'new-e2e-token-only-not-a-real-credential';
  await page.getByLabel('新的访问令牌', { exact: true }).fill(newToken);
  let release!: () => void;
  state.configResponseGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const rejectedPoll = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/v1/mailboxes' && response.status() === 401,
  );
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect.poll(() => state.activeToken).toBe(newToken);
  await rejectedPoll;
  await expect(page.getByRole('button', { name: '正在保存…', exact: true })).toBeVisible();
  release();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  await expect(page.getByRole('status')).toContainText('配置已保存，访问令牌已立即更新');
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).not.toContain(
    newToken,
  );
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
  await page.getByLabel('访问令牌', { exact: true }).fill(newToken);
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await expect(page.getByLabel('新的访问令牌', { exact: true })).toHaveValue('');
  await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible();
});

test('preserves a failed token draft and resumes normal authorization checks', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  const newToken = 'new-e2e-token-only-not-a-real-credential';
  await page.getByLabel('新的访问令牌', { exact: true }).fill(newToken);
  state.configFailure = 'CONFIG_WRITE_FAILED';
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('配置未能写入文件');
  await expect(page.getByLabel('新的访问令牌', { exact: true })).toHaveValue(newToken);
  expect(state.activeToken).toBe(token);
  state.activeToken = 'externally-rotated-e2e-token-only';
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('alert')).toContainText('访问令牌无效或已变更');
});

test('preserves the current session when a blank replacement token is submitted', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('新的访问令牌', { exact: true }).fill(' '.repeat(24));
  await page.getByLabel('邮件同步间隔（秒）', { exact: true }).fill('45');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.getByText('配置已保存到 config.yaml，在线设置已生效。', { exact: true })).toBeVisible();
  expect(state.configWrites[0].postDataJSON().app).not.toHaveProperty('api_token');
  expect(state.activeToken).toBe(token);
  await expect(page.getByLabel('新的访问令牌', { exact: true })).toHaveValue('');
});

test('edits a single provider in a dialog and persists limits and proxy changes', async ({
  page,
}, testInfo) => {
  const state = await mockApi(page);
  await connect(page, '/providers');
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 TempMail.lol', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('请求超时（秒）', { exact: true })).toHaveValue('15');
  await dialog.getByLabel('请求超时（秒）', { exact: true }).fill('25');
  await dialog.getByLabel('最长本地有效期（秒）', { exact: true }).fill('172800');
  const proxy = 'http://e2e-user:e2e-password@proxy.example.test:8080';
  await dialog.getByLabel('替换代理地址', { exact: true }).fill(proxy);
  await expect(dialog.getByLabel('替换代理地址', { exact: true })).toHaveAttribute('type', 'password');
  await page.screenshot({
    path: testInfo.outputPath('desktop-provider-dialog.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.configWrites).toHaveLength(1);
  const firstWrite = state.configWrites[0].postDataJSON();
  expect(firstWrite.providers[1]).toMatchObject({
    enabled: false,
    timeout_seconds: 25,
    max_ttl_seconds: 172800,
    proxy,
  });
  expect(firstWrite.providers[0]).not.toHaveProperty('proxy');
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).not.toContain(
    proxy,
  );
  await page.reload();
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  await expect(dialog.getByLabel('请求超时（秒）', { exact: true })).toHaveValue('25');
  await expect(dialog.getByLabel('最长本地有效期（秒）', { exact: true })).toHaveValue('172800');
  await expect(dialog.getByLabel('替换代理地址', { exact: true })).toHaveValue('');
  await dialog.getByRole('checkbox', { name: '移除已配置代理', exact: true }).check();
  await expect(dialog.getByLabel('替换代理地址', { exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.configWrites[1].postDataJSON().providers[1].proxy).toBeNull();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  await expect(dialog.getByLabel('最长本地有效期（秒）', { exact: true })).toHaveValue('172800');
  await expect(dialog.getByRole('checkbox', { name: '移除已配置代理', exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect
    .poll(async () => {
      const bounds = await dialog.boundingBox();
      return !!bounds && bounds.y >= 0 && bounds.y + bounds.height <= 844;
    })
    .toBe(true);
  await page.screenshot({
    path: testInfo.outputPath('mobile-provider-dialog.png'),
    animations: 'disabled',
  });
});

test('keeps saved provider switch unchanged after a failed write', async ({ page }) => {
  const state = await mockApi(page);
  state.configFailure = 'CONFIG_WRITE_FAILED';
  await connect(page, '/providers');
  const toggle = page.getByRole('switch', { name: '启用 TempMail.lol', exact: true });
  await page.locator('.provider-enable').filter({ has: toggle }).click();
  await expect(
    page.getByText('配置未能写入文件，请检查文件权限和磁盘空间后重试。', { exact: true }),
  ).toBeVisible();
  await expect(toggle).not.toBeChecked();
  expect(state.config.providers[1].enabled).toBe(false);
  state.configFailure = undefined;
  await toggle.press('Space');
  await expect(toggle).toBeChecked();
});

test('preserves provider dialog draft on conflicts and confirms cancellation', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/providers');
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 TempMail.lol', exact: true });
  await dialog.getByLabel('最长本地有效期（秒）', { exact: true }).fill('7200');
  state.config.revision = 'external-edit-revision';
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(
    dialog.getByText('配置已被其他窗口或程序修改。请重新读取配置，再合并你的修改。', { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByLabel('最长本地有效期（秒）', { exact: true })).toHaveValue('7200');
  expect(state.config.providers[1].max_ttl_seconds).toBe(600);
  page.once('dialog', (event) => event.dismiss());
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toBeVisible();
  page.once('dialog', (event) => event.accept());
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toHaveCount(0);
});

test('reorders providers in the editor and rejects out-of-range lifetime before saving', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/providers');
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 TempMail.lol', exact: true });
  await dialog.getByLabel('选择优先级', { exact: true }).click();
  await page.getByRole('option', { name: '第 1 位', exact: true }).click();
  await dialog.getByLabel('最长本地有效期（秒）', { exact: true }).fill('59');
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  expect(state.configWrites).toHaveLength(0);
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('最长本地有效期（秒）', { exact: true }).fill('600');
  await dialog.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.configWrites[0].postDataJSON().providers.map((item: { id: string }) => item.id)).toEqual([
    'tempmail-lol',
    'temp-mail-org',
  ]);
  await page.reload();
  await page.getByRole('button', { name: '编辑 TempMail.lol', exact: true }).click();
  await expect(dialog.getByLabel('选择优先级', { exact: true })).toContainText('第 1 位');
});

test('offers configured long lifetimes when loaded by the running service', async ({ page }) => {
  const state = await mockApi(page);
  await page.route('**/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        providers: [{ id: 'temp-mail-org', capabilities: { ...capabilities, max_ttl_seconds: 172800 } }],
      },
    }),
  );
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByLabel('有效期', { exact: true }).click();
  await page.getByRole('option', { name: '2 天（最长）', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  expect(state.posts[0].postDataJSON().ttl_seconds).toBe(172800);
});

test('preserves config drafts on conflicts and disk write failures', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  const interval = page.getByLabel('邮件同步间隔（秒）', { exact: true });
  await interval.fill('60');
  state.config.revision = 'external-edit-revision';
  state.config.worker.sync_interval_seconds = 90;
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(
    page.getByText('配置已被其他窗口或程序修改。请重新读取配置，再合并你的修改。', { exact: true }),
  ).toBeVisible();
  await expect(interval).toHaveValue('60');
  expect(state.config.worker.sync_interval_seconds).toBe(90);
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: '重新读取配置', exact: true }).click();
  await expect(interval).toHaveValue('90');
  await interval.fill('60');
  state.configFailure = 'CONFIG_WRITE_FAILED';
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(
    page.getByText('配置未能写入文件，请检查文件权限和磁盘空间后重试。', { exact: true }),
  ).toBeVisible();
  await expect(interval).toHaveValue('60');
  expect(state.config.worker.sync_interval_seconds).toBe(90);
  state.configFailure = undefined;
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.getByText('配置已保存到 config.yaml，在线设置已生效。', { exact: true })).toBeVisible();
  expect(state.config.worker.sync_interval_seconds).toBe(60);
});

test('confirms before discarding config drafts through navigation', async ({ page }) => {
  await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/changed.db');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('link', { name: /^供应商/ }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByLabel('数据库路径', { exact: true })).toHaveValue('./data/changed.db');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('link', { name: /^供应商/ }).click();
  await expect(page).toHaveURL(/\/providers$/);
});

test('disconnects when configuration authorization expires', async ({ page }) => {
  await mockApi(page);
  await page.route('**/v1/config', (route) =>
    route.fulfill({ status: 401, json: { error: { code: 'UNAUTHORIZED' } } }),
  );
  await connect(page);
  await page.getByRole('link', { name: /^系统配置/ }).click();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
});

test('keeps configuration drafts when browser back and forward are cancelled', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  await page.getByRole('link', { name: /^系统配置/ }).click();
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/draft.db');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.goBack();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByLabel('数据库路径', { exact: true })).toHaveValue('./data/draft.db');
  page.once('dialog', (dialog) => dialog.accept());
  await page.goBack();
  await expect(page).toHaveURL(/\/inbox$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/settings$/);
  await page.getByRole('link', { name: /^供应商/ }).click();
  await page.goBack();
  await expect(page).toHaveURL(/\/settings$/);
  await page.getByLabel('数据库路径', { exact: true }).fill('./data/second-draft.db');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.goForward();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByLabel('数据库路径', { exact: true })).toHaveValue('./data/second-draft.db');
  page.once('dialog', (dialog) => dialog.accept());
  await page.goForward();
  await expect(page).toHaveURL(/\/providers$/);
});

test('blocks duplicate config saves while writing', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page, '/settings');
  await page.getByLabel('邮件同步间隔（秒）', { exact: true }).fill('60');
  let release!: () => void;
  let writes = 0;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/v1/config', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    writes++;
    await waiting;
    await route.fallback();
  });
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.getByRole('button', { name: '正在保存…', exact: true })).toBeDisabled();
  await expect(page.getByLabel('邮件同步间隔（秒）', { exact: true })).toBeDisabled();
  await page.locator('.config-form').evaluate((form) => (form as HTMLFormElement).requestSubmit());
  await page.getByRole('link', { name: /^供应商/ }).click();
  await expect(page).toHaveURL(/\/settings$/);
  release();
  await expect(page.getByText('配置已保存到 config.yaml，在线设置已生效。', { exact: true })).toBeVisible();
  expect(writes).toBe(1);
  expect(state.configWrites).toHaveLength(1);
});

test('rejects an invalid token without opening the workspace', async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto('/');
  await page.screenshot({ path: testInfo.outputPath('desktop-login.png'), fullPage: true });
  await page.getByLabel('访问令牌', { exact: true }).fill('invalid-e2e-token');
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await expect(page.getByText('访问令牌无效或已变更，请重新连接。', { exact: true })).toBeVisible();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
});

test('reads a message as plain text without running HTML', async ({ page }, testInfo) => {
  await mockApi(page);
  await connect(page);
  await page.screenshot({ path: testInfo.outputPath('desktop-inbox.png'), fullPage: true });
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByText(message.text, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => '__mailExecuted' in window)).toBe(false);
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop-message.png'), fullPage: true });
});

test('creates a mailbox through an asynchronous operation and refreshes the list', async ({ page }) => {
  const state = await mockApi(page);
  state.completeOperation = true;
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toBeVisible({
    timeout: 10_000,
  });
  expect(state.posts).toHaveLength(1);
  expect(state.posts[0].postDataJSON()).toEqual({
    provider: 'auto',
    ttl_seconds: 3600,
    required_capabilities: ['receive'],
  });
  expect(state.posts[0].headers()['idempotency-key']).toBeTruthy();
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible();
});

for (const [path, label] of [
  ['/providers', '供应商'],
  ['/settings', '系统配置'],
  ['/operations', '操作记录'],
] as const) {
  test(`keeps direct ${path} navigation through login and reload`, async ({ page }) => {
    await mockApi(page);
    await connect(page, path);
    const navigation = page.getByRole('navigation', { name: '主导航' });
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(navigation.getByRole('link', { name: new RegExp(`^${label}`) })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(navigation.getByRole('link', { name: new RegExp(`^${label}`) })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.getByLabel('访问令牌', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '新建邮箱', exact: true })).toHaveCount(0);
  });
}

test('updates URLs and supports browser history without duplicating the current page', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  await expect(page).toHaveURL(/\/inbox$/);
  const navigation = page.getByRole('navigation', { name: '主导航' });
  const inbox = navigation.getByRole('link', { name: '邮箱工作台', exact: true });
  const providerLink = navigation.getByRole('link', { name: /^供应商/ });
  const operationsLink = navigation.getByRole('link', { name: /^操作记录/ });
  await expect(inbox).toHaveAttribute('href', '/inbox');
  await expect(providerLink).toHaveAttribute('href', '/providers');
  await expect(operationsLink).toHaveAttribute('href', '/operations');
  await providerLink.click();
  await expect(page).toHaveURL(/\/providers$/);
  await providerLink.click();
  await operationsLink.click();
  await expect(page).toHaveURL(/\/operations$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/providers$/);
  await expect(providerLink).toHaveAttribute('aria-current', 'page');
  await page.goBack();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(inbox).toHaveAttribute('aria-current', 'page');
  await page.goForward();
  await expect(page).toHaveURL(/\/providers$/);
  await expect(providerLink).toHaveAttribute('aria-current', 'page');
});

test('keeps completed operations on their route after reload and opens a mailbox explicitly', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.completeOperation = true;
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await expect(page).toHaveURL(/\/operations$/);
  await page.reload();
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible();
  await expect(page).toHaveURL(/\/operations$/);
  await page.getByRole('button', { name: '查看邮箱', exact: true }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('link', { name: /^供应商/ }).click();
  await page.reload();
  await expect(page.getByText('temp-mail-org', { exact: true }).first()).toBeVisible();
  await expect(page).toHaveURL(/\/providers$/);
});

test('loads server operation history without session IDs after login, reload, logout, and in a new tab', async ({
  page,
  context,
}) => {
  const state = await mockApi(page);
  state.operations = [
    operation({
      id: 'op_server_history',
      status: 'succeeded',
      mailbox_id: 'mailbox-1',
      result: { mailbox_id: 'mailbox-1', email: 'inbox@example.test' },
    }),
  ];
  await page.goto('/operations');
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.operations'))).toBeNull();
  await connect(page, '/operations');
  await expect(page.locator('.operation-row')).toHaveCount(1);
  await expect(page.locator('.operation-row')).toContainText('op_server_history');
  await expect(page.locator('.operation-row')).toContainText('已完成');
  await expect(page).toHaveURL(/\/operations$/);
  expect(
    state.operationQueries.some(
      (url) => url.searchParams.get('offset') === '0' && url.searchParams.get('limit') === '5',
    ),
  ).toBe(true);

  await page.reload();
  await expect(page.locator('.operation-row')).toContainText('op_server_history');
  await expect(page).toHaveURL(/\/operations$/);
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.operations'))).toBeNull();
  await connect(page, '/operations');
  await expect(page.locator('.operation-row')).toContainText('op_server_history');
  await expect(page).toHaveURL(/\/operations$/);

  const otherPage = await context.newPage();
  const otherState = await mockApi(otherPage);
  otherState.operations = state.operations;
  await otherPage.goto('/operations');
  expect(await otherPage.evaluate(() => sessionStorage.getItem('temp-mail.operations'))).toBeNull();
  await connect(otherPage, '/operations');
  await expect(otherPage.locator('.operation-row')).toContainText('op_server_history');
  await expect(otherPage).toHaveURL(/\/operations$/);
  await otherPage.getByRole('button', { name: '查看邮箱', exact: true }).click();
  await expect(otherPage).toHaveURL(/\/inbox$/);
  await expect(otherPage.getByRole('button', { name: 'inbox@example.test', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await otherPage.close();
});

test('pages through server operation history and returns to recent records', async ({ page }) => {
  const state = await mockApi(page);
  state.operations = Array.from({ length: 13 }, (_, index) =>
    operation({
      id: `op_history_${String(index + 1).padStart(2, '0')}`,
      status: 'failed',
      created_at: new Date(Date.now() - index * 60_000).toISOString(),
    }),
  );
  await connect(page, '/operations');
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_01');
  await expect(page.getByRole('button', { name: /^上一页/ })).toBeDisabled();
  await page.getByRole('button', { name: /^下一页/ }).click();
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_06');
  await page.getByRole('button', { name: /^下一页/ }).click();
  await expect(page.locator('.operation-row')).toHaveCount(3);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_11');
  await expect(page.locator('.operation-row').last()).toContainText('op_history_13');
  await expect(page.getByRole('button', { name: /^下一页/ })).toBeDisabled();
  expect(state.operationQueries.some((url) => url.searchParams.get('offset') === '10')).toBe(true);
  expect(state.operationQueries.every((url) => url.searchParams.get('limit') === '5')).toBe(true);
  await page.getByRole('button', { name: /^上一页/ }).click();
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_06');
  await page.getByRole('button', { name: /^上一页/ }).click();
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_01');
  await expect(page.getByRole('button', { name: /^上一页/ })).toBeDisabled();
  await expect(page).toHaveURL(/\/operations$/);
});

test('disconnects when the server operation history rejects the token', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  await page.route(/\/v1\/operations(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 401, json: { error: { code: 'UNAUTHORIZED' } } }),
  );
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('访问令牌无效或已变更');
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
});

test('shows a retryable server operation history failure and restores records', async ({ page }) => {
  const state = await mockApi(page);
  state.operations = [operation({ id: 'op_retry_history', status: 'failed' })];
  let failHistory = true;
  await page.route(/\/v1\/operations(?:\?.*)?$/, (route) =>
    failHistory
      ? route.fulfill({ status: 503, json: { error: { code: 'SERVICE_UNAVAILABLE' } } })
      : route.fallback(),
  );
  await connect(page, '/operations');
  const notice = page.getByRole('alert');
  await expect(notice).toBeVisible();
  failHistory = false;
  await notice.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.locator('.operation-row')).toContainText('op_retry_history');
  await expect(notice).toHaveCount(0);
  await expect(page).toHaveURL(/\/operations$/);
});

test('looks up older server history without navigating and safely returns to all activity on mobile', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await mockApi(page);
  state.operations = Array.from({ length: 22 }, (_, index) =>
    operation({
      id: `op_history_${String(index + 1).padStart(2, '0')}`,
      status: index >= 20 ? 'succeeded' : 'failed',
      mailbox_id: index >= 20 ? 'mailbox-1' : null,
      result: index >= 20 ? { mailbox_id: 'mailbox-1', email: 'inbox@example.test' } : null,
    }),
  );
  await connect(page, '/operations');
  await page.getByRole('button', { name: /界面语言/ }).click();
  await page.getByRole('option', { name: 'English', exact: true }).click();
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await page.getByRole('button', { name: /^Next page/ }).click();
  await expect(page.locator('.operation-row').first()).toContainText('op_history_06');
  await expect(page.locator('.operation-row').filter({ hasText: 'op_history_21' })).toHaveCount(0);
  await page.getByLabel('Operation ID', { exact: true }).fill('op_history_21');
  await page.getByRole('button', { name: 'Find operation', exact: true }).click();
  await expect(page.locator('.operation-row')).toHaveCount(1);
  await expect(page.locator('.operation-row')).toContainText('op_history_21');
  await expect(page.locator('.operation-row')).toContainText('Completed');
  await expect(page).toHaveURL(/\/operations$/);
  const returnToAll = page.getByRole('button', { name: 'Back to all activity', exact: true });
  await expect(returnToAll).toBeEnabled();
  const searchBounds = await page.locator('.operation-search').boundingBox();
  const returnBounds = await returnToAll.boundingBox();
  expect(searchBounds).not.toBeNull();
  expect(returnBounds).not.toBeNull();
  expect(returnBounds!.x).toBeGreaterThanOrEqual(searchBounds!.x - 1);
  expect(returnBounds!.x + returnBounds!.width).toBeLessThanOrEqual(
    searchBounds!.x + searchBounds!.width + 1,
  );
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath('mobile-english-operation-lookup.png'), fullPage: true });

  let releaseLookup!: () => void;
  const lookupGate = new Promise<void>((resolve) => {
    releaseLookup = resolve;
  });
  let lookupStarted = false;
  await page.route('**/v1/operations/op_history_22', async (route) => {
    lookupStarted = true;
    await lookupGate;
    await route.fulfill({ json: state.operations[21] });
  });
  await page.getByLabel('Operation ID', { exact: true }).fill('op_history_22');
  await page.getByRole('button', { name: 'Find operation', exact: true }).click();
  await expect.poll(() => lookupStarted).toBe(true);
  try {
    await expect(returnToAll).toBeDisabled();
  } finally {
    releaseLookup();
  }
  await expect(page.locator('.operation-row')).toContainText('op_history_22');
  await expect(page).toHaveURL(/\/operations$/);
  await expect(returnToAll).toBeEnabled();
  await returnToAll.click();
  await expect(page.locator('.operation-row')).toHaveCount(5);
  await expect(page.locator('.operation-row').first()).toContainText('op_history_06');
  await expect(page.getByRole('button', { name: /^Previous page/ })).toBeEnabled();
  await expect(returnToAll).toHaveCount(0);
  await expect(page).toHaveURL(/\/operations$/);
});

test('updates the route from the operation banner and closes mobile navigation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await page.getByRole('button', { name: '查看操作', exact: true }).click();
  await expect(page).toHaveURL(/\/operations$/);
  await page.getByRole('button', { name: '打开菜单', exact: true }).click();
  await page.getByRole('link', { name: /^供应商/ }).click();
  await expect(page).toHaveURL(/\/providers$/);
  await expect(page.getByRole('button', { name: '关闭菜单', exact: true })).toHaveCount(0);
  await expect(page.getByText('temp-mail-org', { exact: true }).first()).toBeVisible();
});

test('limits duration choices to the selected provider capabilities', async ({ page }) => {
  const state = await mockApi(page);
  state.config.providers[1].enabled = true;
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByLabel('邮箱供应商', { exact: true }).click();
  await page.getByRole('option', { name: 'TempMail.lol', exact: true }).click();
  await page.getByLabel('有效期', { exact: true }).click();
  await expect(page.getByRole('option', { name: '1 小时', exact: true })).toHaveCount(0);
  await expect(page.getByRole('option', { name: '30 分钟', exact: true })).toHaveCount(0);
  await page.getByRole('option', { name: '10 分钟', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  expect(state.posts[0].postDataJSON()).toEqual({
    provider: 'tempmail-lol',
    ttl_seconds: 600,
    required_capabilities: ['receive'],
  });
});

test('retries an interrupted creation with the same idempotency key and payload', async ({ page }) => {
  const state = await mockApi(page);
  state.abortNextPost = true;
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  await expect(
    page.getByText('暂时无法连接服务，请检查网络和 API 是否正常运行。', { exact: true }),
  ).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '继续上次创建', exact: true }).click();
  state.completeOperation = true;
  await page.getByRole('dialog').getByRole('button', { name: '继续上次创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(2);
  expect(state.posts[1].headers()['idempotency-key']).toBe(state.posts[0].headers()['idempotency-key']);
  expect(state.posts[1].postDataJSON()).toEqual(state.posts[0].postDataJSON());
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toBeVisible({
    timeout: 10_000,
  });
});

test('restores an interrupted creation after reload without changing its request', async ({ page }) => {
  const state = await mockApi(page);
  state.config.providers[1].enabled = true;
  state.abortNextPost = true;
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByLabel('邮箱供应商', { exact: true }).click();
  await page.getByRole('option', { name: 'TempMail.lol', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法连接服务');
  const originalDraft = await page.evaluate(() => sessionStorage.getItem('temp-mail.draft'));
  expect(originalDraft).not.toBeNull();
  await page.reload();
  await page.getByRole('button', { name: '继续上次创建', exact: true }).click();
  await expect(page.getByLabel('邮箱供应商', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('有效期', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('邮箱供应商', { exact: true })).toContainText('TempMail.lol');
  await expect(page.getByLabel('有效期', { exact: true })).toContainText('10 分钟');
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.draft'))).toBe(originalDraft);
  state.completeOperation = true;
  await page.getByRole('dialog').getByRole('button', { name: '继续上次创建', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(2);
  expect(state.posts[1].headers()['idempotency-key']).toBe(state.posts[0].headers()['idempotency-key']);
  expect(state.posts[1].postDataJSON()).toEqual({
    provider: 'tempmail-lol',
    ttl_seconds: 600,
    required_capabilities: ['receive'],
  });
  expect(state.posts[1].postDataJSON()).toEqual(state.posts[0].postDataJSON());
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.draft'))).toBeNull();
});

test('opens the created mailbox when an unknown operation later succeeds', async ({ page }) => {
  const state = await mockApi(page);
  state.operation = operation({ status: 'unknown' });
  await connect(page);
  await page.getByRole('button', { name: '新建邮箱', exact: true }).click();
  await page.getByRole('button', { name: '确认创建', exact: true }).click();
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await expect(page.getByText('结果待确认', { exact: true })).toBeVisible();
  expect(state.posts).toHaveLength(1);
  state.completeOperation = true;
  await expect(page.getByRole('button', { name: 'created@example.test', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
    { timeout: 10_000 },
  );
  await expect(page.getByText('邮箱已创建，可以开始收件了。', { exact: true })).toBeVisible();
  expect(state.posts).toHaveLength(1);
});

test('disconnects when a manual operation lookup rejects the token', async ({ page }) => {
  await mockApi(page);
  await page.route('**/v1/operations/lookup-unauthorized', (route) =>
    route.fulfill({ status: 401, json: { error: { code: 'UNAUTHORIZED' } } }),
  );
  await connect(page);
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await page.getByLabel('操作 ID', { exact: true }).fill('lookup-unauthorized');
  await page.getByRole('button', { name: '查询操作', exact: true }).click();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('访问令牌无效或已变更');
  expect(await page.evaluate(() => sessionStorage.getItem('temp-mail.token'))).toBeNull();
});

test('does not restore operation state when a lookup completes after logout', async ({ page }) => {
  await mockApi(page);
  let releaseResponse!: () => void;
  const gate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let lookupStarted = false;
  await page.route('**/v1/operations/lookup-delayed', async (route) => {
    lookupStarted = true;
    await gate;
    await route.fulfill({ json: operation({ id: 'lookup-delayed' }) });
  });
  await connect(page);
  await page.getByRole('link', { name: /^操作记录/ }).click();
  await page.getByLabel('操作 ID', { exact: true }).fill('lookup-delayed');
  await page.getByRole('button', { name: '查询操作', exact: true }).click();
  await expect.poll(() => lookupStarted).toBe(true);
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  const response = page.waitForResponse('**/v1/operations/lookup-delayed');
  releaseResponse();
  await (await response).finished();
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  );
  for (const key of ['token', 'operations', 'draft']) {
    expect(await page.evaluate((item) => sessionStorage.getItem(`temp-mail.${item}`), key)).toBeNull();
  }
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
});

test('ignores an old mailbox body arriving after another mailbox is selected', async ({ page }) => {
  const state = await mockApi(page);
  state.mailboxes.push(mailbox({ id: 'mailbox-2', email: 'second@example.test' }));
  const secondMessage = {
    ...message,
    id: 'message-2',
    mailbox_id: 'mailbox-2',
    subject: '第二个邮箱的来信',
    text: '只属于第二个邮箱的正文。',
  };
  let releaseOldBody!: () => void;
  const gate = new Promise<void>((resolve) => {
    releaseOldBody = resolve;
  });
  let oldBodyStarted = false;
  let oldBodyFinished = false;
  page.on('requestfinished', (request) => {
    if (request.url().endsWith('/messages/message-1')) oldBodyFinished = true;
  });
  page.on('requestfailed', (request) => {
    if (request.url().endsWith('/messages/message-1')) oldBodyFinished = true;
  });
  await page.route('**/v1/mailboxes/mailbox-1/messages/message-1', async (route) => {
    oldBodyStarted = true;
    await gate;
    await route.fulfill({ json: message });
  });
  await page.route('**/v1/mailboxes/mailbox-2/messages?*', (route) =>
    route.fulfill({
      json: { items: [secondMessage], limit: 20, offset: 0, last_synced_at: new Date().toISOString() },
    }),
  );
  await page.route('**/v1/mailboxes/mailbox-2/messages/message-2', (route) =>
    route.fulfill({ json: secondMessage }),
  );
  await connect(page);
  await page.getByText(message.subject, { exact: true }).click();
  await expect.poll(() => oldBodyStarted).toBe(true);
  await page.getByRole('button', { name: 'second@example.test', exact: true }).click();
  await page.getByText(secondMessage.subject, { exact: true }).click();
  await expect(page.getByTestId('message-body')).toHaveText(secondMessage.text);
  releaseOldBody();
  await expect.poll(() => oldBodyFinished).toBe(true);
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  );
  await expect(page.getByTestId('message-body')).toHaveText(secondMessage.text);
  await expect(page.getByRole('button', { name: 'second@example.test', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.getByText(message.text, { exact: true })).toHaveCount(0);
});

test('keeps expired mailbox messages readable after refresh, navigation, and reload', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByText(message.text, { exact: true })).toBeVisible();
  state.mailboxes[0] = mailbox({ status: 'expired', expires_at: new Date(Date.now() - 1000).toISOString() });
  await page.getByRole('button', { name: '刷新邮箱', exact: true }).click();
  await expect(page.getByTestId('message-body')).toHaveText(message.text);
  await expect(page.getByText('邮箱已过期，已停止收发，历史邮件仍可查看。', { exact: true })).toBeVisible();
  await expect(page.getByText('已过期', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: '复制邮件正文', exact: true })).toBeVisible();
  await page.getByRole('link', { name: /^供应商/ }).click();
  await page.getByRole('link', { name: '邮箱工作台', exact: true }).click();
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByTestId('message-body')).toHaveText(message.text);
  await page.reload();
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByTestId('message-body')).toHaveText(message.text);
  await expect(page.getByText('邮箱已过期，已停止收发，历史邮件仍可查看。', { exact: true })).toBeVisible();
  expect(state.cleanupPosts).toHaveLength(0);
  expect(state.mailboxes).toHaveLength(1);
});

test('explains that an expired empty mailbox has no historical messages and is no longer receiving', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes = [
    mailbox({
      id: 'expired-empty',
      email: 'empty-expired@example.test',
      status: 'expired',
      expires_at: new Date(Date.now() - 60_000).toISOString(),
    }),
  ];
  await connect(page);
  await page.getByRole('button', { name: 'empty-expired@example.test', exact: true }).click();
  const messages = page.getByRole('region', { name: '邮件列表', exact: true });
  await expect(
    messages.getByText('邮箱已过期，已停止收发，历史邮件仍可查看。', { exact: true }),
  ).toBeVisible();
  await expect(messages.getByRole('heading', { name: '没有历史邮件', exact: true })).toBeVisible();
  await expect(
    messages.getByText('这个邮箱没有已保存的邮件。需要继续收件时，请创建一个新邮箱。', { exact: true }),
  ).toBeVisible();
  await expect(messages.getByText('等待第一封来信', { exact: true })).toHaveCount(0);
  await expect(
    messages.getByText('复制邮箱地址并发送邮件，新的来信会自动出现在这里。', { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByTestId('message-body')).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(0);
});

test('retains expired message content until cleanup is confirmed and preserves its operation history afterward', async ({
  page,
}) => {
  const state = await mockApi(page);
  state.mailboxes[0] = mailbox({
    status: 'expired',
    expires_at: new Date(Date.now() - 60_000).toISOString(),
  });
  state.operations = [
    operation({
      id: 'op_retained_expired_message',
      status: 'succeeded',
      mailbox_id: 'mailbox-1',
      result: { mailbox_id: 'mailbox-1', email: 'inbox@example.test' },
    }),
  ];
  await connect(page);
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByTestId('message-body')).toHaveText(message.text);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '清除失效邮箱', exact: true });
  await expect(dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByTestId('message-body')).toHaveText(message.text);
  expect(state.cleanupPosts).toHaveLength(0);
  await page.getByRole('button', { name: '清除失效邮箱', exact: true }).click();
  await dialog.getByRole('button', { name: '确认清除 1 个邮箱', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '已清除 1 个失效邮箱。' })).toBeVisible();
  await expect(page.getByTestId('message-body')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'inbox@example.test', exact: true })).toHaveCount(0);
  expect(state.cleanupPosts).toHaveLength(1);
  expect(state.mailboxes).toHaveLength(0);
  await page.getByRole('link', { name: /^操作记录/ }).click();
  const row = page.locator('.operation-row').filter({ hasText: 'op_retained_expired_message' });
  await expect(row).toContainText('inbox@example.test');
  await expect(row).toContainText('已完成');
  await expect(row.getByRole('button', { name: '查看邮箱', exact: true })).toHaveCount(0);
  expect(state.operations[0].mailbox_id).toBeNull();
  expect(state.operations[0].result?.mailbox_id).toBe('mailbox-1');
});

test('stores credentials in session only and clears workspace state on logout', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('temp-mail.token') ?? 'null'))).toBe(
    token,
  );
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(token);
  await page.evaluate(() => {
    sessionStorage.setItem('temp-mail.operations', JSON.stringify(['operation-1']));
    sessionStorage.setItem('temp-mail.draft', JSON.stringify({ key: 'a-draft-key' }));
  });
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByLabel('访问令牌', { exact: true })).toBeVisible();
  for (const key of ['token', 'operations', 'draft']) {
    expect(await page.evaluate((item) => sessionStorage.getItem(`temp-mail.${item}`), key)).toBeNull();
  }
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(token);
});

test('shows configured provider capabilities without promising unsupported sending', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  await page.getByRole('link', { name: /^供应商/ }).click();
  await expect(page.getByText('temp-mail-org', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('tempmail-lol', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('10 分钟', { exact: true })).toBeVisible();
  await expect(page.getByText('1 小时', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /发送邮件/ })).toHaveCount(0);
});

for (const total of [10, 11]) {
  test(`requests five-item mailbox pagination and complete address search with ${total} mailboxes`, async ({
    page,
  }) => {
    const state = await mockApi(page);
    state.mailboxes = Array.from({ length: total }, (_, index) =>
      mailbox({ id: `mailbox-${index + 1}`, email: `inbox-${index + 1}@example.test` }),
    );
    await connect(page);
    const next = page.getByRole('button', { name: '下一页邮箱', exact: true });
    const previous = page.getByRole('button', { name: '上一页邮箱', exact: true });
    await expect(page.locator('.mailbox-row')).toHaveCount(5);
    await expect(page.getByRole('button', { name: 'inbox-1@example.test', exact: true })).toBeVisible();
    await expect(previous).toBeDisabled();
    await next.click();
    await expect(page.locator('.mailbox-row')).toHaveCount(5);
    await expect(page.getByRole('button', { name: 'inbox-6@example.test', exact: true })).toBeVisible();
    if (total === 11) {
      await next.click();
      await expect(page.locator('.mailbox-row')).toHaveCount(1);
      await expect(page.getByRole('button', { name: 'inbox-11@example.test', exact: true })).toBeVisible();
    }
    await expect(next).toBeDisabled();
    expect(state.queries.some((url) => url.searchParams.get('offset') === '5')).toBe(true);
    expect(state.queries.every((url) => url.searchParams.get('limit') === '5')).toBe(true);
    if (total === 11) {
      await previous.click();
      await expect(page.getByRole('button', { name: 'inbox-6@example.test', exact: true })).toBeVisible();
    }
    await previous.click();
    await expect(page.locator('.mailbox-row')).toHaveCount(5);
    await expect(page.getByRole('button', { name: 'inbox-1@example.test', exact: true })).toBeVisible();
    await expect(previous).toBeDisabled();
    await next.click();
    await expect(page.getByRole('button', { name: 'inbox-6@example.test', exact: true })).toBeVisible();
    await page.getByLabel('搜索邮箱', { exact: true }).fill('inbox-3@example.test');
    await page.getByLabel('搜索邮箱', { exact: true }).press('Enter');
    await expect
      .poll(() =>
        state.queries.some(
          (url) =>
            url.searchParams.get('email') === 'inbox-3@example.test' &&
            url.searchParams.get('offset') === '0',
        ),
      )
      .toBe(true);
    await expect(page.getByRole('button', { name: 'inbox-3@example.test', exact: true })).toBeVisible();
    await expect(page.locator('.mailbox-row')).toHaveCount(1);
    await expect(next).toHaveCount(0);
    await page.getByLabel('搜索邮箱', { exact: true }).fill('');
    await page.getByLabel('搜索邮箱', { exact: true }).press('Enter');
    await expect(page.locator('.mailbox-row')).toHaveCount(5);
    await expect(page.getByRole('button', { name: 'inbox-1@example.test', exact: true })).toBeVisible();
    await expect(previous).toBeDisabled();
  });
}

test('keeps the mailbox workspace usable on a narrow screen', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByText(message.text, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-message.png'), fullPage: true });
});

test('keeps manual refresh busy until both mailbox and message requests finish', async ({
  page,
}, testInfo) => {
  await mockApi(page);
  await connect(page);
  await expect(page.getByText(message.subject, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '自动刷新', exact: true }).click();
  let releaseMailboxes!: () => void;
  let releaseMessages!: () => void;
  const mailboxesGate = new Promise<void>((resolve) => {
    releaseMailboxes = resolve;
  });
  const messagesGate = new Promise<void>((resolve) => {
    releaseMessages = resolve;
  });
  let mailboxStarted = false;
  let messagesStarted = false;
  await page.route('**/v1/mailboxes?*', async (route) => {
    mailboxStarted = true;
    await mailboxesGate;
    await route.fallback();
  });
  await page.route('**/v1/mailboxes/mailbox-1/messages?*', async (route) => {
    messagesStarted = true;
    await messagesGate;
    await route.fallback();
  });
  const refresh = page.getByRole('button', { name: '刷新邮箱', exact: true });
  await refresh.click();
  await expect.poll(() => mailboxStarted && messagesStarted).toBe(true);
  await expect(refresh).toHaveAttribute('data-state', 'refreshing');
  await expect(refresh).toHaveAttribute('data-pending', 'true');
  await expect(refresh).toBeDisabled();
  await expect(refresh).toContainText('刷新中');
  await expect(refresh.locator('svg.spin')).toBeVisible();
  expect(
    await refresh.locator('svg.spin').evaluate((element) => getComputedStyle(element).animationName),
  ).not.toBe('none');
  await page.screenshot({ path: testInfo.outputPath('desktop-refreshing.png'), fullPage: true });
  releaseMailboxes();
  // The minimum visual feedback interval must not end an unfinished request.
  await page.waitForTimeout(850);
  await expect(refresh).toHaveAttribute('data-state', 'refreshing');
  await expect(refresh).toBeDisabled();
  releaseMessages();
  await expect(refresh).toHaveAttribute('data-state', 'done');
  await expect(refresh).toContainText('已刷新');
  await expect(refresh).toBeEnabled();
  await expect(refresh.locator('svg.spin')).toHaveCount(0);
});

test('keeps quick refresh feedback visible and does not announce background polls', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page);
  await expect(page.getByText(message.subject, { exact: true })).toBeVisible();
  const refresh = page.getByRole('button', { name: '刷新邮箱', exact: true });
  const started = await page.evaluate(() => performance.now());
  await refresh.click();
  await expect(refresh).toHaveAttribute('data-state', 'refreshing');
  await expect(refresh).toBeDisabled();
  await expect(refresh).toHaveAttribute('data-state', 'done');
  const elapsed = (await page.evaluate(() => performance.now())) - started;
  expect(elapsed).toBeGreaterThanOrEqual(600);
  await expect(refresh).toHaveAttribute('data-state', 'idle');
  const previousRequests = state.queries.length;
  await expect.poll(() => state.queries.length, { timeout: 7000 }).toBeGreaterThan(previousRequests);
  await expect(refresh).toHaveAttribute('data-state', 'idle');
  await expect(refresh).not.toContainText('已刷新');
  await expect(refresh).not.toContainText('刷新失败');
});

test('shows a failed refresh honestly and allows a successful retry', async ({ page }) => {
  await mockApi(page);
  await connect(page);
  await expect(page.getByText(message.subject, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '自动刷新', exact: true }).click();
  let failRefresh = true;
  await page.route('**/v1/mailboxes?*', async (route) => {
    if (failRefresh) {
      await route.fulfill({ status: 503, json: { error: { code: 'SERVICE_UNAVAILABLE' } } });
    } else {
      await route.fallback();
    }
  });
  const refresh = page.getByRole('button', { name: '刷新邮箱', exact: true });
  await refresh.click();
  await expect(refresh).toHaveAttribute('data-state', 'failed');
  await expect(refresh).toContainText('刷新失败');
  await expect(refresh).not.toContainText('已刷新');
  await expect(refresh).toBeEnabled();
  failRefresh = false;
  await refresh.click();
  await expect(refresh).toHaveAttribute('data-state', 'done');
  await expect(refresh).toContainText('已刷新');
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('confirms copying the actual address and clears feedback when the selected mailbox changes', async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => {
    const writes: string[] = [];
    Object.defineProperty(window, '__clipboardWrites', { value: writes });
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: async (value: string) => {
          writes.push(value);
        },
      },
    });
  });
  const state = await mockApi(page);
  state.mailboxes.push(mailbox({ id: 'mailbox-2', email: 'second@example.test' }));
  await connect(page);
  await page.getByRole('button', { name: '复制邮箱地址', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '已复制' })).toBeVisible();
  expect(
    await page.evaluate(() => (window as Window & { __clipboardWrites?: string[] }).__clipboardWrites),
  ).toEqual(['inbox@example.test']);
  await page.screenshot({ path: testInfo.outputPath('desktop-copied.png'), fullPage: true });
  await page.getByRole('button', { name: 'second@example.test', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '已复制' })).toHaveCount(0);
  await page.getByRole('button', { name: '复制邮箱地址', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '已复制' })).toBeVisible();
  expect(
    await page.evaluate(() => (window as Window & { __clipboardWrites?: string[] }).__clipboardWrites),
  ).toEqual(['inbox@example.test', 'second@example.test']);
  await page.getByRole('link', { name: /^供应商/ }).click();
  await page.getByRole('link', { name: '邮箱工作台', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '已复制' })).toHaveCount(0);
});

test('explains clipboard failure without confirming a copy', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: async () => {
          throw new Error('Clipboard denied');
        },
      },
    });
  });
  await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: '复制邮箱地址', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('请手动选择复制');
  await expect(page.getByRole('status').filter({ hasText: '已复制' })).toHaveCount(0);
});

test('respects reduced motion during manual refresh on a narrow screen', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await connect(page);
  await expect(page.getByText(message.subject, { exact: true })).toBeVisible();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/v1/mailboxes?*', async (route) => {
    await gate;
    await route.fallback();
  });
  const refresh = page.getByRole('button', { name: '刷新邮箱', exact: true });
  await refresh.click();
  await expect(refresh).toHaveAttribute('data-state', 'refreshing');
  await expect(refresh).toBeDisabled();
  const animation = await refresh.locator('svg.spin').evaluate((element) => {
    const style = getComputedStyle(element);
    return { name: style.animationName, duration: parseFloat(style.animationDuration) };
  });
  expect(animation.name === 'none' || animation.duration <= 0.001).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-refreshing.png'), fullPage: true });
  release();
  await expect(refresh).toHaveAttribute('data-state', 'done');
});
