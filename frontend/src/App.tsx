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
  LayoutDashboard,
  LogOut,
  Mail,
  Menu,
  Plus,
  RefreshCw,
  ShieldCheck,
  Settings2,
  X,
} from 'lucide-react';
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent,
} from 'react';
import { api, ApiError, errorMessage } from './api';
import { Brand, ErrorNotice, Loading, RefreshButton } from './components/Common';
import Inbox from './components/Inbox';
import Login from './components/Login';
import AppearanceControls from './components/AppearanceControls';
import CreateMailbox from './components/CreateMailbox';
import CleanupMailboxes from './components/CleanupMailboxes';
import { useI18n } from './preferences';
import { sectionPaths, useSection, type Section } from './routing';
import { clearSession, readSession, useResource, writeSession } from './state';
import type { Configuration, CreatePayload, Draft, Operation } from './types';

const ConfigEditor = lazy(() => import('./components/ConfigEditor'));
const Providers = lazy(() => import('./components/Providers'));
const Dashboard = lazy(() => import('./components/Dashboard'));
const Operations = lazy(() =>
  import('./components/Overview').then((module) => ({ default: module.Operations })),
);

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
  useI18n();
  const [token, setToken] = useState(() => {
    const stored = readSession<unknown>('token', '');
    return typeof stored === 'string' ? stored : '';
  });
  const [reason, setReason] = useState('');
  const disconnect = useCallback((message = '') => {
    clearSession();
    setToken('');
    setReason((current) => (current === 'TOKEN_UPDATED' && message === 'UNAUTHORIZED' ? current : message));
  }, []);
  if (!token)
    return (
      <>
        <Login
          reason={reason === 'TOKEN_UPDATED' ? '' : reason}
          onConnect={(value) => {
            clearSession();
            writeSession('token', value);
            setToken(value);
            setReason('');
          }}
        />
        {reason === 'TOKEN_UPDATED' && (
          <div className="toast" role="status">
            <ShieldCheck size={17} />
            <span>{errorMessage(reason)}</span>
          </div>
        )}
      </>
    );
  return <Workspace key={token} token={token} disconnect={disconnect} />;
}

function Workspace({ token, disconnect }: { token: string; disconnect: (message?: string) => void }) {
  const t = useI18n();
  const headings = {
    index: [
      t('首页', 'Home'),
      t('每一份动态，一目了然。', 'Your workspace at a glance.'),
      t(
        '查看邮箱概况、收件趋势和最近活动。',
        'Review your mailboxes, incoming mail trends, and recent activity.',
      ),
    ],
    inbox: [
      t('邮箱工作台', 'Inbox'),
      t('你的收件箱，轻装上阵。', 'Your inbox, without the clutter.'),
      t(
        '创建一个临时地址，让每一封来信各归其位。',
        'Create a temporary address and keep every message in its place.',
      ),
    ],
    providers: [
      t('供应商', 'Providers'),
      t('多个来源，一个入口。', 'Multiple providers. One workspace.'),
      t(
        '管理已支持的邮箱服务，按需开启和调整配置。',
        'Manage supported email services and configure them to suit your needs.',
      ),
    ],
    operations: [
      t('操作记录', 'Activity'),
      t('看得见的每一步。', 'Every step, in view.'),
      t(
        '查看邮箱创建、发送与删除的处理进度和历史结果。',
        'Review progress and history for mailbox creation, sending, and deletion.',
      ),
    ],
    settings: [
      t('系统配置', 'Settings'),
      t('让工作空间，按你的方式运行。', 'Make this workspace your own.'),
      t(
        '管理存储、访问与后台同步，保存到本地配置文件。',
        'Manage storage, access and background sync in your local configuration.',
      ),
    ],
  } satisfies Record<Section, string[]>;

  const [section, navigate] = useSection();
  const [menuOpen, setMenuOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  const [draft, setDraft] = useState<Draft | null>(restoreDraft);
  const draftRef = useRef(draft);
  const submitting = useRef(false);
  const mounted = useRef(true);
  const tokenChangePending = useRef(false);
  const [authCheck, setAuthCheck] = useState(0);
  const onTokenChangePending = useCallback((pending: boolean) => {
    tokenChangePending.current = pending;
    if (!pending && mounted.current) setAuthCheck((value) => value + 1);
  }, []);
  const [operationIds, setOperationIds] = useState(restoreIds);
  const [operationOffset, setOperationOffset] = useState(0);
  const [operationLookupId, setOperationLookupId] = useState<string | null>(null);
  const operationCache = useRef(new Map<string, Operation>());
  const handled = useRef(new Set<string>());
  const restoredOperations = useRef(new Set(operationIds));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messageId, setMessageId] = useState<string | null>(null);
  const [mailboxOffset, setMailboxOffset] = useState(0);
  const [messageOffset, setMessageOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [searchEmail, setSearchEmail] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [now, setNow] = useState(Date.now());
  const [toast, setToast] = useState<'created' | 'submitted' | 'cleaned' | ''>('');
  const [cleanedCount, setCleanedCount] = useState(0);
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
    section === 'index' ? null : `mailboxes:${mailboxOffset}:${searchEmail}`,
    (signal) => api.mailboxes(token, mailboxOffset, searchEmail, signal),
    autoRefresh ? 5000 : 0,
  );
  const selectedMailbox = mailboxes.data?.items.find((item) => item.id === selectedId);
  const mailboxActive = selectedMailbox?.status === 'active' && Date.parse(selectedMailbox.expires_at) > now;
  const mailboxReadable = selectedMailbox && selectedMailbox.status !== 'deleted';
  const messages = useResource(
    mailboxReadable ? `messages:${selectedId}:${messageOffset}` : null,
    (signal) => api.messages(token, selectedId!, messageOffset, signal),
    autoRefresh && mailboxActive ? 5000 : 0,
  );
  const message = useResource(
    mailboxReadable && messageId ? `message:${selectedId}:${messageId}` : null,
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
            return { item: cached, error: { id, cause: error } };
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
  const history = useResource(
    section === 'operations' && !operationLookupId ? `operation-history:${operationOffset}` : null,
    (signal) => api.operations(token, operationOffset, signal),
    2000,
  );
  const lookupOperation = operationLookupId
    ? (tracked.find((item) => item.id === operationLookupId) ?? operationCache.current.get(operationLookupId))
    : null;
  const pending = tracked.filter((item) => ['pending', 'running'].includes(item.status));
  const providerList = providers.data?.providers ?? [];
  const globalError = [
    providers.error,
    mailboxes.error,
    messages.error,
    message.error,
    operations.error,
    history.error,
  ].find((error) => error instanceof ApiError && error.status === 401);
  useEffect(() => {
    if (globalError instanceof ApiError && !tokenChangePending.current) disconnect(globalError.code);
  }, [globalError, disconnect, authCheck]);
  const onConfigurationSaved = useCallback(
    (_config: Configuration, options?: { tokenChanged: boolean }) => {
      if (options?.tokenChanged) disconnect('TOKEN_UPDATED');
      else providers.refresh();
    },
    [disconnect, providers.refresh],
  );

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
      navigate('inbox');
      mailboxes.refresh();
    },
    [mailboxes.refresh, selectMailbox, navigate],
  );
  useEffect(() => {
    // Previously completed operations must not redirect a restored page on reload.
    tracked.forEach((item) => {
      if (restoredOperations.current.delete(item.id) && ['succeeded', 'failed'].includes(item.status)) {
        handled.current.add(item.id);
      }
    });
    const finished = tracked.filter(
      (item) => ['succeeded', 'failed'].includes(item.status) && !handled.current.has(item.id),
    );
    finished.forEach((item) => handled.current.add(item.id));
    const success = finished.find((item) => item.status === 'succeeded' && item.mailbox_id);
    if (success) {
      openMailbox(success.mailbox_id!, success.result?.email);
      setToast('created');
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
      setToast('submitted');
    } catch (error) {
      if (!mounted.current) return;
      setCreateError(error);
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        draftRef.current = null;
        setDraft(null);
        writeSession('draft', null);
        if (error.status === 401) disconnect(error.code);
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
    history.refresh();
    setNow(Date.now());
  }
  const heading = headings[section];
  const loadedActive = (mailboxes.data?.items ?? []).filter(
    (item) => item.status === 'active' && Date.parse(item.expires_at) > now,
  ).length;
  const connectionError = !!providers.error || !!mailboxes.error;
  function chooseSection(event: MouseEvent<HTMLAnchorElement>, value: Section) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(value);
    setMenuOpen(false);
  }
  return (
    <div className="app-shell">
      {menuOpen && (
        <button
          className="mobile-scrim"
          aria-label={t('关闭菜单', 'Close menu')}
          onClick={() => setMenuOpen(false)}
        />
      )}
      <aside className={`sidebar ${menuOpen ? 'open' : ''}`}>
        <Brand />
        <div className="workspace-label">
          {t('个人工作空间', 'Personal workspace')}
          <Chip size="sm" variant="soft">
            WORKSPACE
          </Chip>
        </div>
        <span className="nav-caption">{t('工作台', 'Workspace')}</span>
        <nav aria-label={t('主导航', 'Main navigation')}>
          <a
            href={sectionPaths.index}
            aria-current={section === 'index' ? 'page' : undefined}
            className={`button button--tertiary nav-link ${section === 'index' ? 'active' : ''}`}
            onClick={(event) => chooseSection(event, 'index')}
          >
            <LayoutDashboard size={19} />
            <span>{t('首页', 'Home')}</span>
          </a>
          <a
            href={sectionPaths.inbox}
            aria-current={section === 'inbox' ? 'page' : undefined}
            className={`button button--tertiary nav-link ${section === 'inbox' ? 'active' : ''}`}
            onClick={(event) => chooseSection(event, 'inbox')}
          >
            <InboxIcon size={19} />
            <span>{t('邮箱工作台', 'Inbox')}</span>
          </a>
          <a
            href={sectionPaths.providers}
            aria-current={section === 'providers' ? 'page' : undefined}
            className={`button button--tertiary nav-link ${section === 'providers' ? 'active' : ''}`}
            onClick={(event) => chooseSection(event, 'providers')}
          >
            <Layers size={19} />
            <span>{t('供应商', 'Providers')}</span>
            <span className="nav-count">{providerList.length}</span>
          </a>
          <a
            href={sectionPaths.operations}
            aria-current={section === 'operations' ? 'page' : undefined}
            className={`button button--tertiary nav-link ${section === 'operations' ? 'active' : ''}`}
            onClick={(event) => chooseSection(event, 'operations')}
          >
            <History size={19} />
            <span>{t('操作记录', 'Activity')}</span>
            {pending.length > 0 && <span className="nav-count">{pending.length}</span>}
          </a>
          <a
            href={sectionPaths.settings}
            aria-current={section === 'settings' ? 'page' : undefined}
            className={`button button--tertiary nav-link ${section === 'settings' ? 'active' : ''}`}
            onClick={(event) => chooseSection(event, 'settings')}
          >
            <Settings2 size={19} />
            <span>{t('系统配置', 'Settings')}</span>
          </a>
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-tip">
            <div>
              <ShieldCheck size={18} />
              <strong>{t('短暂的地址，隐私的收件', 'Temporary addresses, private inbox')}</strong>
            </div>
          </div>
          <a className="sidebar-docs" href="/docs" target="_blank" rel="noreferrer">
            <Code2 size={17} />
            <span>{t('API 文档', 'API docs')}</span>
            <ArrowUpRight size={14} />
          </a>
          <div className="workspace-owner">
            <span className="owner-avatar">T</span>
            <div>
              <strong>{t('本地工作空间', 'Local workspace')}</strong>
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
              aria-label={t('打开菜单', 'Open menu')}
              onPress={() => setMenuOpen(true)}
            >
              <Menu size={19} />
            </Button>
            <span>{t('工作台', 'Workspace')}</span>
            <ChevronRight size={13} />
            <strong>{heading[0]}</strong>
          </div>
          <div className="header-actions">
            <span className={`connection-status ${connectionError ? 'offline' : ''}`}>
              <span />
              {connectionError
                ? t('连接异常', 'Connection error')
                : providers.loading && !providers.data
                  ? t('连接中', 'Connecting')
                  : t('API 已连接', 'API connected')}
            </span>
            <AppearanceControls />
            <span className="header-divider" />
            <Button
              size="sm"
              variant="tertiary"
              onPress={() => disconnect()}
              aria-label={t('退出登录', 'Log out')}
            >
              <LogOut size={16} />
              <span className="logout-label">{t('退出', 'Log out')}</span>
            </Button>
          </div>
        </header>
        <main className="main-content page-transition" id="main-content" key={section}>
          <div className="page-heading">
            <div>
              <div className="page-eyebrow">
                <span />
                TEMP MAIL / {section.toUpperCase()}
              </div>
              <h1>{heading[1]}</h1>
              <p>{heading[2]}</p>
            </div>
            {section === 'inbox' && (
              <Button className="primary-action" onPress={() => setCreateOpen(true)}>
                <Plus size={18} />
                {draft ? t('继续上次创建', 'Resume creation') : t('新建邮箱', 'New mailbox')}
              </Button>
            )}
          </div>
          <ErrorNotice error={providers.error} retry={providers.refresh} />
          {section === 'index' && (
            <Suspense fallback={<Loading />}>
              <Dashboard
                token={token}
                disconnect={disconnect}
                openMailbox={openMailbox}
                navigate={navigate}
              />
            </Suspense>
          )}
          {section === 'inbox' && (
            <>
              <div className="stats-row">
                <div className="stat">
                  <div className="stat-icon peach">
                    <Mail size={20} />
                  </div>
                  <div>
                    <span>
                      {t('使用中的邮箱', 'Active mailboxes')} <small>{t('本页', 'This page')}</small>
                    </span>
                    <strong>{mailboxes.data ? String(loadedActive).padStart(2, '0') : '—'}</strong>
                  </div>
                  <span className="stat-detail">{t('随时准备收件', 'Ready to receive')}</span>
                </div>
                <div className="stat">
                  <div className="stat-icon violet">
                    <Layers size={20} />
                  </div>
                  <div>
                    <span>{t('已接入供应商', 'Available providers')}</span>
                    <strong>{providers.data ? String(providerList.length).padStart(2, '0') : '—'}</strong>
                  </div>
                  <span className="stat-detail">{t('统一管理', 'All in one place')}</span>
                </div>
                <div className="stat">
                  <div className="stat-icon green">
                    <RefreshCw size={19} />
                  </div>
                  <div>
                    <span>{t('页面自动刷新', 'Auto-refresh')}</span>
                    <strong className="stat-text">
                      {autoRefresh ? t('每 5 秒', 'Every 5 seconds') : t('已暂停', 'Paused')}
                    </strong>
                  </div>
                  <span className="stat-detail">{t('读取本地收件', 'Reads cached mail')}</span>
                </div>
              </div>
              {(pending.length > 0 ||
                tracked.some((item) => ['failed', 'unknown'].includes(item.status))) && (
                <div className="operation-banner">
                  <Clock3 size={17} />
                  <span>
                    {pending.length
                      ? t(
                          `${pending.length} 个邮箱正在创建，完成后会自动显示。`,
                          `${pending.length} mailbox request(s) in progress. New mailboxes appear when ready.`,
                        )
                      : t(
                          '有创建请求未完成，可在操作记录中查看原因。',
                          'Some requests are incomplete. Check Activity for details.',
                        )}
                  </span>
                  <Button size="sm" variant="tertiary" onPress={() => navigate('operations')}>
                    {t('查看操作', 'View activity')}
                    <ArrowUpRight size={14} />
                  </Button>
                </div>
              )}
              <div className="inbox-section-heading">
                <div>
                  <h2>{t('所有邮箱', 'All mailboxes')}</h2>
                  <span>{t('每一封来信，都有一个位置。', 'A place for every message.')}</span>
                </div>
                <div className="inbox-controls">
                  <CleanupMailboxes
                    token={token}
                    onUnauthorized={disconnect}
                    onCleared={(count) => {
                      setMailboxOffset(0);
                      selectMailbox('');
                      refresh();
                      setCleanedCount(count);
                      setToast('cleaned');
                    }}
                  />
                  <Button
                    size="sm"
                    variant="tertiary"
                    aria-pressed={autoRefresh}
                    onPress={() => setAutoRefresh((value) => !value)}
                  >
                    <span className={`toggle-track ${autoRefresh ? 'on' : ''}`}>
                      <span />
                    </span>
                    <span>{t('自动刷新', 'Auto-refresh')}</span>
                  </Button>
                  <RefreshButton
                    refresh={refresh}
                    loading={[providers, mailboxes, messages, message, operations].some(
                      (resource) => resource.loading,
                    )}
                    failed={
                      [providers, mailboxes, messages, message, operations].some(
                        (resource) => !!resource.error,
                      ) || !!operations.data?.errors.length
                    }
                  />
                </div>
              </div>
              {searchEmail && (
                <div className="search-filter">
                  {t('正在查询：', 'Searching: ')}
                  <strong>{searchEmail}</strong>
                  <Button
                    size="sm"
                    variant="tertiary"
                    onPress={() => {
                      setQuery('');
                      setSearchEmail('');
                      setMailboxOffset(0);
                    }}
                  >
                    {t('清除查询', 'Clear search')}
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
                  {t(
                    '消息由后台定期同步，页面刷新不会直接请求供应商。',
                    'Mail syncs in the background. Refreshing reads the local cache.',
                  )}
                </span>
                <a href="/docs" target="_blank" rel="noreferrer">
                  <CircleHelp size={13} />
                  {t('接口说明', 'API reference')}
                  <ArrowUpRight size={12} />
                </a>
              </div>
            </>
          )}
          {section === 'providers' && (
            <Suspense fallback={<Loading />}>
              <Providers token={token} disconnect={disconnect} onSaved={onConfigurationSaved} />
            </Suspense>
          )}
          {section === 'settings' && (
            <Suspense fallback={<Loading />}>
              <ConfigEditor
                token={token}
                disconnect={disconnect}
                onSaved={onConfigurationSaved}
                onTokenChangePending={onTokenChangePending}
              />
            </Suspense>
          )}
          {section === 'operations' && (
            <Suspense fallback={<Loading />}>
              <Operations
                operations={
                  operationLookupId ? (lookupOperation ? [lookupOperation] : []) : (history.data?.items ?? [])
                }
                loading={operationLookupId ? operations.loading : history.loading}
                error={
                  operationLookupId
                    ? (operations.error ??
                      operations.data?.errors
                        .filter(({ id }) => id === operationLookupId)
                        .map(({ id, cause }) => `${id}: ${errorMessage(cause)}`)
                        .join('; '))
                    : history.error
                }
                retry={operationLookupId ? operations.refresh : history.refresh}
                offset={operationOffset}
                total={history.data?.total ?? 0}
                changePage={setOperationOffset}
                filtered={!!operationLookupId}
                clearFilter={() => setOperationLookupId(null)}
                addOperation={async (id) => {
                  try {
                    const item = await api.operation(token, id);
                    if (mounted.current) {
                      // Looking up completed history must not open a mailbox automatically.
                      if (['succeeded', 'failed'].includes(item.status)) handled.current.add(item.id);
                      track(item);
                      setOperationLookupId(item.id);
                    }
                  } catch (error) {
                    if (mounted.current && error instanceof ApiError && error.status === 401)
                      disconnect(error.code);
                    throw error;
                  }
                }}
                openMailbox={openMailbox}
              />
            </Suspense>
          )}
        </main>
      </div>
      <CreateMailbox
        open={createOpen}
        setOpen={setCreateOpen}
        providers={providerList}
        draft={draft}
        busy={creating}
        error={createError}
        submit={create}
      />
      {toast && (
        <div role="status" className="toast">
          <ShieldCheck size={17} />
          <span>
            {toast === 'cleaned'
              ? t(`已清除 ${cleanedCount} 个失效邮箱。`, `Cleared ${cleanedCount} expired mailbox(es).`)
              : toast === 'created'
                ? t('邮箱已创建，可以开始收件了。', 'Mailbox created. You can start receiving mail.')
                : t(
                    '创建请求已提交，正在等待后台处理。',
                    'Request submitted. Waiting for background processing.',
                  )}
          </span>
          <Button
            isIconOnly
            size="sm"
            variant="tertiary"
            aria-label={t('关闭提示', 'Dismiss notification')}
            onPress={() => setToast('')}
          >
            <X size={14} />
          </Button>
        </div>
      )}
    </div>
  );
}
