import { Button, Chip } from '@heroui/react';
import {
  ArrowUpRight,
  ChevronRight,
  CircleHelp,
  Clock3,
  Code2,
  History,
  Inbox as InboxIcon,
  Layers,
  LogOut,
  Mail,
  Menu,
  Plus,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, errorMessage } from './api';
import { Brand, ErrorNotice, Loading } from './components/Common';
import Inbox from './components/Inbox';
import Login from './components/Login';
import { clearSession, readSession, useResource, writeSession } from './state';
import type { CreatePayload, Draft, Operation } from './types';

const CreateMailbox = lazy(() => import('./components/CreateMailbox'));
const Providers = lazy(() =>
  import('./components/Overview').then((module) => ({ default: module.Providers })),
);
const Operations = lazy(() =>
  import('./components/Overview').then((module) => ({ default: module.Operations })),
);

type Section = 'inbox' | 'providers' | 'operations';
const headings = {
  inbox: ['邮箱工作台', '你的收件箱，轻装上阵。', '创建一个临时地址，让每一封来信各归其位。'],
  providers: ['供应商', '多个来源，一个入口。', '了解已接入的邮箱服务，以及它们支持的能力。'],
  operations: ['操作记录', '看得见的每一步。', '从提交到完成，跟踪邮箱创建的处理进度。'],
} satisfies Record<Section, string[]>;

function restoreDraft(): Draft | null {
  const value = readSession<Draft | null>('draft', null);
  return value &&
    typeof value.key === 'string' &&
    typeof value.payload?.provider === 'string' &&
    Number.isInteger(value.payload.ttl_seconds) &&
    value.payload.ttl_seconds >= 60
    ? value
    : null;
}
function restoreIds(): string[] {
  const value = readSession<unknown>('operations', []);
  return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [];
}

export default function App() {
  const [token, setToken] = useState(() => {
    const stored = readSession<unknown>('token', '');
    return typeof stored === 'string' ? stored : '';
  });
  const [reason, setReason] = useState('');
  const disconnect = useCallback((message = '') => {
    clearSession();
    setToken('');
    setReason(message);
  }, []);
  if (!token)
    return (
      <Login
        reason={reason}
        onConnect={(value) => {
          clearSession();
          writeSession('token', value);
          setToken(value);
          setReason('');
        }}
      />
    );
  return <Workspace key={token} token={token} disconnect={disconnect} />;
}

function Workspace({ token, disconnect }: { token: string; disconnect: (message?: string) => void }) {
  const [section, setSection] = useState<Section>('inbox');
  const [menuOpen, setMenuOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  const [draft, setDraft] = useState<Draft | null>(restoreDraft);
  const draftRef = useRef(draft);
  const submitting = useRef(false);
  const mounted = useRef(true);
  const [operationIds, setOperationIds] = useState(restoreIds);
  const operationCache = useRef(new Map<string, Operation>());
  const handled = useRef(new Set<string>());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messageId, setMessageId] = useState<string | null>(null);
  const [mailboxOffset, setMailboxOffset] = useState(0);
  const [messageOffset, setMessageOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [searchEmail, setSearchEmail] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [now, setNow] = useState(Date.now());
  const [toast, setToast] = useState('');
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(''), 5000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const providers = useResource(
    'providers',
    (signal) => api.providers(token, signal),
    autoRefresh ? 30_000 : 0,
  );
  const mailboxes = useResource(
    `mailboxes:${mailboxOffset}:${searchEmail}`,
    (signal) => api.mailboxes(token, mailboxOffset, searchEmail, signal),
    autoRefresh ? 5000 : 0,
  );
  const selectedMailbox = mailboxes.data?.items.find((item) => item.id === selectedId);
  const mailboxActive = selectedMailbox?.status === 'active' && Date.parse(selectedMailbox.expires_at) > now;
  const messages = useResource(
    mailboxActive ? `messages:${selectedId}:${messageOffset}` : null,
    (signal) => api.messages(token, selectedId!, messageOffset, signal),
    autoRefresh ? 5000 : 0,
  );
  const message = useResource(
    mailboxActive && messageId ? `message:${selectedId}:${messageId}` : null,
    (signal) => api.message(token, selectedId!, messageId!, signal),
  );
  const operations = useResource(
    operationIds.length ? `operations:${operationIds.join(',')}` : null,
    async (signal) => {
      const results = await Promise.all(
        operationIds.map(async (id) => {
          const cached = operationCache.current.get(id);
          if (cached && ['succeeded', 'failed'].includes(cached.status)) return { item: cached };
          try {
            const item = await api.operation(token, id, signal);
            if (!signal.aborted) operationCache.current.set(id, item);
            return { item };
          } catch (error) {
            if (error instanceof ApiError && error.status === 401) throw error;
            if (signal.aborted) throw error;
            return { item: cached, error: `${id}：${errorMessage(error)}` };
          }
        }),
      );
      return {
        items: results.flatMap((result) => (result.item ? [result.item] : [])),
        errors: results.flatMap((result) => (result.error ? [result.error] : [])),
      };
    },
    2000,
  );
  const tracked = operations.data?.items ?? [];
  const pending = tracked.filter((item) => ['pending', 'running'].includes(item.status));
  const providerList = providers.data?.providers ?? [];
  const globalError = [
    providers.error,
    mailboxes.error,
    messages.error,
    message.error,
    operations.error,
  ].find((error) => error instanceof ApiError && error.status === 401);
  useEffect(() => {
    if (globalError) disconnect('访问令牌无效或已变更，请重新连接。');
  }, [globalError, disconnect]);

  const selectMailbox = useCallback((id: string) => {
    setSelectedId(id);
    setMessageId(null);
    setMessageOffset(0);
  }, []);
  useEffect(() => {
    if (mailboxes.data && (!selectedId || !mailboxes.data.items.some((item) => item.id === selectedId))) {
      const first = mailboxes.data.items.find((item) => item.status === 'active') ?? mailboxes.data.items[0];
      if (first) selectMailbox(first.id);
      else {
        setSelectedId(null);
        setMessageId(null);
      }
    }
  }, [mailboxes.data, selectedId, selectMailbox]);
  const openMailbox = useCallback(
    (id: string, email?: string) => {
      setMailboxOffset(0);
      setQuery(email ?? '');
      setSearchEmail(email ?? '');
      selectMailbox(id);
      setSection('inbox');
      mailboxes.refresh();
    },
    [mailboxes.refresh, selectMailbox],
  );
  useEffect(() => {
    const finished = tracked.filter(
      (item) => ['succeeded', 'failed'].includes(item.status) && !handled.current.has(item.id),
    );
    finished.forEach((item) => handled.current.add(item.id));
    const success = finished.find(
      (item) => item.status === 'succeeded' && (item.mailbox_id || item.result?.mailbox_id),
    );
    if (success) {
      openMailbox((success.mailbox_id ?? success.result?.mailbox_id)!, success.result?.email);
      setToast('邮箱已创建，可以开始收件了。');
    }
  }, [operations.data, openMailbox]);

  function track(item: Operation) {
    operationCache.current.set(item.id, item);
    setOperationIds((current) => {
      const ids = [item.id, ...current.filter((id) => id !== item.id)];
      writeSession('operations', ids);
      return ids;
    });
  }
  async function create(payload: CreatePayload) {
    if (submitting.current) return;
    submitting.current = true;
    setCreating(true);
    setCreateError(null);
    const current = draftRef.current ?? { key: crypto.randomUUID(), payload };
    draftRef.current = current;
    setDraft(current);
    writeSession('draft', current);
    try {
      const operation = await api.create(token, current.payload, current.key);
      if (!mounted.current) return;
      draftRef.current = null;
      setDraft(null);
      writeSession('draft', null);
      track(operation);
      setCreateOpen(false);
      setToast('创建请求已提交，正在等待后台处理。');
    } catch (error) {
      if (!mounted.current) return;
      setCreateError(error);
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        draftRef.current = null;
        setDraft(null);
        writeSession('draft', null);
        if (error.status === 401) disconnect(error.message);
      }
    } finally {
      submitting.current = false;
      if (mounted.current) setCreating(false);
    }
  }
  function search(event: FormEvent) {
    event.preventDefault();
    setMailboxOffset(0);
    selectMailbox('');
    setSearchEmail(query.trim());
    mailboxes.refresh();
  }
  function refresh() {
    providers.refresh();
    mailboxes.refresh();
    messages.refresh();
    message.refresh();
    operations.refresh();
    setNow(Date.now());
  }
  const heading = headings[section];
  const loadedActive = (mailboxes.data?.items ?? []).filter(
    (item) => item.status === 'active' && Date.parse(item.expires_at) > now,
  ).length;
  const connectionError = !!providers.error || !!mailboxes.error;
  function chooseSection(value: Section) {
    setSection(value);
    setMenuOpen(false);
  }
  return (
    <div className="app-shell">
      {menuOpen && (
        <button className="mobile-scrim" aria-label="关闭菜单" onClick={() => setMenuOpen(false)} />
      )}
      <aside className={`sidebar ${menuOpen ? 'open' : ''}`}>
        <Brand />
        <div className="workspace-label">
          个人工作空间
          <Chip size="sm" variant="soft">
            WORKSPACE
          </Chip>
        </div>
        <span className="nav-caption">工作台</span>
        <nav aria-label="主导航">
          <Button
            variant="tertiary"
            className={`nav-link ${section === 'inbox' ? 'active' : ''}`}
            onPress={() => chooseSection('inbox')}
          >
            <InboxIcon size={19} />
            <span>邮箱工作台</span>
            <ChevronRight size={14} className="nav-arrow" />
          </Button>
          <Button
            variant="tertiary"
            className={`nav-link ${section === 'providers' ? 'active' : ''}`}
            onPress={() => chooseSection('providers')}
          >
            <Layers size={19} />
            <span>供应商</span>
            <span className="nav-count">{providerList.length}</span>
          </Button>
          <Button
            variant="tertiary"
            className={`nav-link ${section === 'operations' ? 'active' : ''}`}
            onPress={() => chooseSection('operations')}
          >
            <History size={19} />
            <span>操作记录</span>
            {pending.length > 0 && <span className="nav-count">{pending.length}</span>}
          </Button>
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-tip">
            <div>
              <ShieldCheck size={18} />
              <strong>短暂的地址，专注的收件</strong>
            </div>
            <p>临时邮箱会在有效期后过期，记得及时保存重要内容。</p>
          </div>
          <a className="sidebar-docs" href="/docs" target="_blank" rel="noreferrer">
            <Code2 size={17} />
            <span>API 文档</span>
            <ArrowUpRight size={14} />
          </a>
          <div className="workspace-owner">
            <span className="owner-avatar">T</span>
            <div>
              <strong>本地工作空间</strong>
              <small>Temp Mail</small>
            </div>
            <span className="tiny-dot" />
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="app-header">
          <div className="breadcrumbs">
            <Button
              isIconOnly
              variant="tertiary"
              size="sm"
              className="mobile-menu"
              aria-label="打开菜单"
              onPress={() => setMenuOpen(true)}
            >
              <Menu size={19} />
            </Button>
            <span>工作台</span>
            <ChevronRight size={13} />
            <strong>{heading[0]}</strong>
          </div>
          <div className="header-actions">
            <span className={`connection-status ${connectionError ? 'offline' : ''}`}>
              <span />
              {connectionError ? '连接异常' : providers.loading && !providers.data ? '连接中' : 'API 已连接'}
            </span>
            <span className="header-divider" />
            <Button size="sm" variant="tertiary" onPress={() => disconnect()} aria-label="退出登录">
              <LogOut size={16} />
              <span className="logout-label">退出</span>
            </Button>
          </div>
        </header>
        <main className="main-content" id="main-content">
          <div className="page-heading">
            <div>
              <div className="page-eyebrow">
                <span />
                TEMP MAIL / {section.toUpperCase()}
              </div>
              <h1>{heading[1]}</h1>
              <p>{heading[2]}</p>
            </div>
            <Button className="primary-action" onPress={() => setCreateOpen(true)}>
              <Plus size={18} />
              {draft ? '继续上次创建' : '新建邮箱'}
            </Button>
          </div>
          <ErrorNotice error={providers.error} retry={providers.refresh} />
          {section === 'inbox' && (
            <>
              <div className="stats-row">
                <div className="stat">
                  <div className="stat-icon peach">
                    <Mail size={20} />
                  </div>
                  <div>
                    <span>
                      使用中的邮箱 <small>本页</small>
                    </span>
                    <strong>{mailboxes.data ? String(loadedActive).padStart(2, '0') : '—'}</strong>
                  </div>
                  <span className="stat-detail">随时准备收件</span>
                </div>
                <div className="stat">
                  <div className="stat-icon violet">
                    <Layers size={20} />
                  </div>
                  <div>
                    <span>已接入供应商</span>
                    <strong>{providers.data ? String(providerList.length).padStart(2, '0') : '—'}</strong>
                  </div>
                  <span className="stat-detail">统一管理</span>
                </div>
                <div className="stat">
                  <div className="stat-icon green">
                    <RefreshCw size={19} />
                  </div>
                  <div>
                    <span>页面自动刷新</span>
                    <strong className="stat-text">{autoRefresh ? '每 5 秒' : '已暂停'}</strong>
                  </div>
                  <span className="stat-detail">读取本地收件</span>
                </div>
              </div>
              {(pending.length > 0 ||
                tracked.some((item) => ['failed', 'unknown'].includes(item.status))) && (
                <div className="operation-banner">
                  <Clock3 size={17} />
                  <span>
                    {pending.length
                      ? `${pending.length} 个邮箱正在创建，完成后会自动显示。`
                      : '有创建请求未完成，可在操作记录中查看原因。'}
                  </span>
                  <Button size="sm" variant="tertiary" onPress={() => setSection('operations')}>
                    查看操作
                    <ArrowUpRight size={14} />
                  </Button>
                </div>
              )}
              <div className="inbox-section-heading">
                <div>
                  <h2>所有邮箱</h2>
                  <span>每一封来信，都有一个位置。</span>
                </div>
                <div className="inbox-controls">
                  <Button
                    size="sm"
                    variant="tertiary"
                    aria-pressed={autoRefresh}
                    onPress={() => setAutoRefresh((value) => !value)}
                  >
                    <span className={`toggle-track ${autoRefresh ? 'on' : ''}`}>
                      <span />
                    </span>
                    <span>自动刷新</span>
                  </Button>
                  <Button size="sm" variant="secondary" onPress={refresh} aria-label="刷新邮箱">
                    <RefreshCw size={14} className={mailboxes.loading ? 'spin' : ''} />
                    刷新
                  </Button>
                </div>
              </div>
              {searchEmail && (
                <div className="search-filter">
                  正在查询：<strong>{searchEmail}</strong>
                  <Button
                    size="sm"
                    variant="tertiary"
                    onPress={() => {
                      setQuery('');
                      setSearchEmail('');
                      setMailboxOffset(0);
                    }}
                  >
                    清除查询
                    <X size={13} />
                  </Button>
                </div>
              )}
              <Inbox
                mailboxes={mailboxes}
                messages={messages}
                message={message}
                selectedMailbox={selectedMailbox}
                selectedMessage={messageId}
                selectMailbox={selectMailbox}
                selectMessage={setMessageId}
                mailboxOffset={mailboxOffset}
                messageOffset={messageOffset}
                setMailboxOffset={(value) => {
                  setMailboxOffset(value);
                  selectMailbox('');
                }}
                setMessageOffset={(value) => {
                  setMessageOffset(value);
                  setMessageId(null);
                }}
                query={query}
                setQuery={setQuery}
                search={search}
                now={now}
                create={() => setCreateOpen(true)}
              />
              <div className="workspace-footer">
                <span>
                  <ShieldCheck size={13} />
                  消息由后台定期同步，页面刷新不会直接请求供应商。
                </span>
                <a href="/docs" target="_blank" rel="noreferrer">
                  <CircleHelp size={13} />
                  接口说明
                  <ArrowUpRight size={12} />
                </a>
              </div>
            </>
          )}
          {section === 'providers' && (
            <Suspense fallback={<Loading />}>
              <Providers providers={providerList} />
            </Suspense>
          )}
          {section === 'operations' && (
            <Suspense fallback={<Loading />}>
              <Operations
                operations={tracked}
                loading={operations.loading}
                error={operations.error ?? operations.data?.errors.join('；')}
                retry={operations.refresh}
                addOperation={async (id) => {
                  try {
                    const item = await api.operation(token, id);
                    if (mounted.current) track(item);
                  } catch (error) {
                    if (mounted.current && error instanceof ApiError && error.status === 401)
                      disconnect(error.message);
                    throw error;
                  }
                }}
                openMailbox={openMailbox}
              />
            </Suspense>
          )}
        </main>
      </div>
      {createOpen && (
        <Suspense fallback={<Loading label="正在打开创建窗口…" />}>
          <CreateMailbox
            open={createOpen}
            setOpen={setCreateOpen}
            providers={providerList}
            draft={draft}
            busy={creating}
            error={createError}
            submit={create}
          />
        </Suspense>
      )}
      {toast && (
        <div role="status" className="toast">
          <ShieldCheck size={17} />
          <span>{toast}</span>
          <Button isIconOnly size="sm" variant="tertiary" aria-label="关闭提示" onPress={() => setToast('')}>
            <X size={14} />
          </Button>
        </div>
      )}
    </div>
  );
}
