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
  mailboxes: Mailbox[];
  operation: Operation;
  posts: Request[];
  queries: URL[];
  detailError?: string;
  abortNextPost?: boolean;
  completeOperation?: boolean;
}

async function mockApi(page: Page): Promise<MockState> {
  const state: MockState = { mailboxes: [mailbox()], operation: operation(), posts: [], queries: [] };
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.headers().authorization !== `Bearer ${token}`) {
      await route.fulfill({ status: 401, json: { error: { code: 'UNAUTHORIZED' } } });
      return;
    }
    if (url.pathname === '/v1/capabilities') {
      await route.fulfill({ json: { providers } });
    } else if (url.pathname === '/v1/mailboxes' && request.method() === 'POST') {
      state.posts.push(request);
      if (state.abortNextPost) {
        state.abortNextPost = false;
        await route.abort('failed');
        return;
      }
      await route.fulfill({ status: 202, json: state.operation });
    } else if (url.pathname === '/v1/mailboxes') {
      state.queries.push(url);
      const email = url.searchParams.get('email');
      const offset = Number(url.searchParams.get('offset') ?? 0);
      const items = state.mailboxes
        .filter((item) => !email || item.email === email)
        .slice(offset, offset + 20);
      await route.fulfill({ json: { items, limit: 20, offset } });
    } else if (url.pathname === '/v1/operations/operation-1') {
      if (state.completeOperation) {
        state.operation = operation({
          status: 'succeeded',
          mailbox_id: 'mailbox-created',
          result: { mailbox_id: 'mailbox-created', email: 'created@example.test' },
        });
        if (!state.mailboxes.some((item) => item.id === 'mailbox-created')) {
          state.mailboxes.unshift(mailbox({ id: 'mailbox-created', email: 'created@example.test' }));
        }
      }
      await route.fulfill({ json: state.operation });
    } else if (/\/messages\/message-1$/.test(url.pathname)) {
      if (state.detailError)
        await route.fulfill({ status: 410, json: { error: { code: state.detailError } } });
      else await route.fulfill({ json: message });
    } else if (/\/messages$/.test(url.pathname)) {
      const selected = state.mailboxes.find((item) => url.pathname.includes(`/${item.id}/`));
      if (selected?.status === 'expired') {
        await route.fulfill({ status: 410, json: { error: { code: 'MAILBOX_EXPIRED' } } });
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

async function connect(page: Page) {
  await page.goto('/');
  await page.getByLabel('访问令牌', { exact: true }).fill(token);
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await expect(page.getByRole('button', { name: '新建邮箱', exact: true })).toBeVisible();
}

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
  await page.getByRole('button', { name: '操作记录', exact: true }).click();
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible();
});

test('limits duration choices to the selected provider capabilities', async ({ page }) => {
  const state = await mockApi(page);
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
  await page.getByRole('button', { name: '取消', exact: true }).click();
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
  await page.getByRole('button', { name: '操作记录', exact: true }).click();
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
  await page.getByRole('button', { name: '操作记录', exact: true }).click();
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
  await page.getByRole('button', { name: '操作记录', exact: true }).click();
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

test('removes the displayed body after the selected mailbox expires', async ({ page }) => {
  const state = await mockApi(page);
  await connect(page);
  await page.getByRole('button', { name: 'inbox@example.test', exact: true }).click();
  await page.getByText(message.subject, { exact: true }).first().click();
  await expect(page.getByText(message.text, { exact: true })).toBeVisible();
  state.mailboxes[0] = mailbox({ status: 'expired', expires_at: new Date(Date.now() - 1000).toISOString() });
  await page.getByRole('button', { name: '刷新邮箱', exact: true }).click();
  await expect(page.getByText(message.text, { exact: true })).toHaveCount(0);
  await expect(page.getByText('已过期', { exact: true }).first()).toBeVisible();
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
  await page.getByRole('button', { name: /^供应商/ }).click();
  await expect(page.getByText('temp-mail-org', { exact: true })).toBeVisible();
  await expect(page.getByText('tempmail-lol', { exact: true })).toBeVisible();
  await expect(page.getByText('10 分钟', { exact: true })).toBeVisible();
  await expect(page.getByText('1 小时', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /发送邮件/ })).toHaveCount(0);
});

test('requests mailbox pagination and complete address search from the API', async ({ page }) => {
  const state = await mockApi(page);
  state.mailboxes = Array.from({ length: 21 }, (_, index) =>
    mailbox({ id: `mailbox-${index + 1}`, email: `inbox-${index + 1}@example.test` }),
  );
  await connect(page);
  await expect(page.getByRole('button', { name: 'inbox-1@example.test', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '下一页邮箱', exact: true }).click();
  await expect(page.getByRole('button', { name: 'inbox-21@example.test', exact: true })).toBeVisible();
  expect(state.queries.some((url) => url.searchParams.get('offset') === '20')).toBe(true);
  await page.getByLabel('搜索邮箱', { exact: true }).fill('inbox-3@example.test');
  await page.getByRole('button', { name: '搜索邮箱地址', exact: true }).click();
  await expect
    .poll(() =>
      state.queries.some(
        (url) =>
          url.searchParams.get('email') === 'inbox-3@example.test' && url.searchParams.get('offset') === '0',
      ),
    )
    .toBe(true);
  await expect(page.getByRole('button', { name: 'inbox-3@example.test', exact: true })).toBeVisible();
});

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
