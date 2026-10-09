import { Button } from '@heroui/react';
import {
  Activity,
  ArrowUpRight,
  CircleAlert,
  Clock3,
  History,
  Inbox,
  Layers,
  Mail,
  ShieldCheck,
} from 'lucide-react';
import { useEffect } from 'react';
import { api, ApiError } from '../api';
import { useI18n } from '../preferences';
import type { Section } from '../routing';
import { formatDate, providerName, useResource } from '../state';
import { Empty, ErrorNotice, Loading, OperationBadge, RefreshButton } from './Common';

export default function Dashboard({
  token,
  disconnect,
  openMailbox,
  navigate,
}: {
  token: string;
  disconnect: (message?: string) => void;
  openMailbox: (id: string, email?: string) => void;
  navigate: (section: Section) => void;
}) {
  const t = useI18n();
  const dashboard = useResource(`dashboard:${token}`, (signal) => api.dashboard(token, signal), 10_000);
  const data = dashboard.data;
  useEffect(() => {
    if (dashboard.error instanceof ApiError && dashboard.error.status === 401)
      disconnect(dashboard.error.code);
  }, [dashboard.error, disconnect]);
  const stats = data
    ? [
        {
          id: 'active',
          title: t('有效邮箱', 'Active mailboxes'),
          value: data.mailboxes.active,
          detail: t(`共 ${data.mailboxes.total} 个邮箱`, `${data.mailboxes.total} mailboxes in total`),
          icon: <Inbox size={21} />,
        },
        {
          id: 'messages',
          title: t('缓存邮件', 'Cached messages'),
          value: data.messages.total,
          detail: t(
            `24 小时内新增 ${data.messages.received_24h} 封`,
            `${data.messages.received_24h} cached in the last 24 hours`,
          ),
          icon: <Mail size={21} />,
        },
        {
          id: 'pending',
          title: t('待处理操作', 'Pending operations'),
          value: data.operations.pending + data.operations.running,
          detail: t(
            `${data.operations.pending} 个等待 · ${data.operations.running} 个处理中`,
            `${data.operations.pending} queued · ${data.operations.running} running`,
          ),
          icon: <Clock3 size={21} />,
        },
        {
          id: 'errors',
          title: t('操作异常', 'Operation issues'),
          value: data.operations.failed + data.operations.unknown,
          detail: t(
            `${data.operations.failed} 个失败 · ${data.operations.unknown} 个待确认`,
            `${data.operations.failed} failed · ${data.operations.unknown} unconfirmed`,
          ),
          icon: <CircleAlert size={21} />,
        },
      ]
    : [];
  const maximum = Math.max(1, ...(data?.activity.flatMap((day) => [day.mailboxes, day.messages]) ?? []));
  const numberFormat = new Intl.NumberFormat(t('zh-CN', 'en-US'));
  const compactFormat = new Intl.NumberFormat(t('zh-CN', 'en-US'), {
    notation: 'compact',
    maximumFractionDigits: 1,
  });
  const providerTotal = data?.provider_stats.reduce((total, provider) => total + provider.mailboxes, 0) ?? 0;
  return (
    <section className="dashboard" aria-label={t('工作空间概览', 'Workspace overview')}>
      <div className="dashboard-toolbar">
        <span className="dashboard-updated">
          <span className="dashboard-update-dot" />
          {data
            ? t(
                `更新于 ${formatDate(data.generated_at, true)}`,
                `Updated ${formatDate(data.generated_at, true)}`,
              )
            : t('每 10 秒更新工作空间数据', 'Workspace data updates every 10 seconds')}
        </span>
        <div className="dashboard-actions">
          <RefreshButton
            label={t('刷新看板', 'Refresh dashboard')}
            refresh={dashboard.refresh}
            loading={dashboard.loading}
            failed={!!dashboard.error}
          />
          <Button variant="secondary" size="sm" onPress={() => navigate('inbox')}>
            {t('进入邮箱工作台', 'Open inbox')}
            <ArrowUpRight size={15} />
          </Button>
        </div>
      </div>
      <ErrorNotice error={dashboard.error} retry={dashboard.refresh} />
      {!data && dashboard.loading && <Loading label={t('正在加载看板…', 'Loading dashboard…')} />}
      {data && (
        <>
          <div className="dashboard-stats">
            {stats.map((stat) => (
              <article className="dashboard-stat" key={stat.id} data-testid={`dashboard-stat-${stat.id}`}>
                <div className="dashboard-stat-heading">
                  <h2>{stat.title}</h2>
                  <span className="dashboard-stat-icon">{stat.icon}</span>
                </div>
                <strong>{numberFormat.format(stat.value)}</strong>
                <p>{stat.detail}</p>
              </article>
            ))}
          </div>
          <div className="dashboard-grid">
            <section className="dashboard-card dashboard-trend" aria-labelledby="dashboard-trend-title">
              <div className="dashboard-card-heading">
                <div>
                  <h2 id="dashboard-trend-title">{t('最近 7 天', 'Last 7 days')}</h2>
                  <p>
                    {t(
                      '按现存数据统计，清除后会减少 · UTC 日期',
                      'Based on retained data; cleanup reduces counts · UTC dates',
                    )}
                  </p>
                </div>
                <Activity size={21} />
              </div>
              <div className="dashboard-legend" aria-hidden="true">
                <span>
                  <i className="dashboard-series-mailboxes" />
                  {t('新增邮箱', 'New mailboxes')}
                </span>
                <span>
                  <i className="dashboard-series-messages" />
                  {t('新增缓存邮件', 'New cached messages')}
                </span>
              </div>
              {data.activity.length ? (
                <div
                  className="dashboard-chart"
                  role="list"
                  aria-label={t('每日新增数量（UTC）', 'Daily new items (UTC)')}
                >
                  {data.activity.map((day) => (
                    <div
                      className="dashboard-chart-day"
                      role="listitem"
                      key={day.date}
                      aria-label={t(
                        `${day.date} UTC：新增邮箱 ${day.mailboxes} 个，新增缓存邮件 ${day.messages} 封`,
                        `${day.date} UTC: ${day.mailboxes} new mailboxes, ${day.messages} new cached messages`,
                      )}
                    >
                      <div className="dashboard-bars" aria-hidden="true">
                        <div className="dashboard-bar-track">
                          <span>{compactFormat.format(day.mailboxes)}</span>
                          <i
                            className="dashboard-series-mailboxes"
                            style={{ height: `${(day.mailboxes / maximum) * 100}%` }}
                          />
                        </div>
                        <div className="dashboard-bar-track">
                          <span>{compactFormat.format(day.messages)}</span>
                          <i
                            className="dashboard-series-messages"
                            style={{ height: `${(day.messages / maximum) * 100}%` }}
                          />
                        </div>
                      </div>
                      <time dateTime={day.date}>{day.date.slice(5).replace('-', '/')}</time>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty
                  title={t('还没有活动数据', 'No activity yet')}
                  description={t(
                    '创建邮箱后，趋势会显示在这里。',
                    'Activity appears here after you create a mailbox.',
                  )}
                />
              )}
            </section>
            <section className="dashboard-card" aria-labelledby="dashboard-providers-title">
              <div className="dashboard-card-heading">
                <div>
                  <h2 id="dashboard-providers-title">{t('供应商分布', 'Mailbox providers')}</h2>
                  <p>{t('各供应商留存的邮箱数量', 'Retained mailboxes by provider')}</p>
                </div>
                <Layers size={21} />
              </div>
              {data.provider_stats.length && providerTotal ? (
                <div className="dashboard-providers">
                  {data.provider_stats.map((provider) => (
                    <div key={provider.id} className="dashboard-provider">
                      <div>
                        <strong>{providerName(provider.id)}</strong>
                        <span>{t(`${provider.mailboxes} 个`, `${provider.mailboxes}`)}</span>
                      </div>
                      <div className="dashboard-provider-track" aria-hidden="true">
                        <span style={{ width: `${(provider.mailboxes / providerTotal) * 100}%` }} />
                      </div>
                      <p>{t(`${provider.active} 个有效邮箱`, `${provider.active} active mailboxes`)}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty
                  title={t('还没有邮箱', 'No mailboxes yet')}
                  description={t(
                    '前往邮箱工作台创建你的第一个临时地址。',
                    'Open the inbox to create your first temporary address.',
                  )}
                />
              )}
              <Button
                className="dashboard-text-link"
                variant="tertiary"
                size="sm"
                onPress={() => navigate('providers')}
              >
                {t('管理供应商', 'Manage providers')}
                <ArrowUpRight size={14} />
              </Button>
            </section>
            <section className="dashboard-card dashboard-recent" aria-labelledby="dashboard-recent-title">
              <div className="dashboard-card-heading">
                <div>
                  <h2 id="dashboard-recent-title">{t('最近操作', 'Recent activity')}</h2>
                  <p>{t('最近 3 条操作及处理结果', 'The latest 3 operations and their results')}</p>
                </div>
                <Button variant="tertiary" size="sm" onPress={() => navigate('operations')}>
                  {t('查看全部', 'View all')}
                  <ArrowUpRight size={14} />
                </Button>
              </div>
              {data.recent_operations.length ? (
                <div className="dashboard-operation-list">
                  {data.recent_operations.slice(0, 3).map((operation) => (
                    <article
                      className="dashboard-operation"
                      key={operation.id}
                      data-testid="dashboard-operation"
                    >
                      <span className="dashboard-operation-icon">
                        <History size={17} />
                      </span>
                      <div className="dashboard-operation-info">
                        <strong>
                          {operation.kind === 'create'
                            ? t('创建邮箱', 'Create mailbox')
                            : operation.kind === 'send'
                              ? t('发送邮件', 'Send message')
                              : t('删除邮箱', 'Delete mailbox')}
                        </strong>
                        <p>
                          {providerName(operation.provider_id)} · {t('更新于', 'Updated')}{' '}
                          {formatDate(operation.updated_at, true)}
                        </p>
                        <code title={operation.id}>{operation.id}</code>
                      </div>
                      <div className="dashboard-operation-actions">
                        <OperationBadge status={operation.status} />
                        {operation.mailbox_id && (
                          <Button
                            size="sm"
                            variant="tertiary"
                            onPress={() => openMailbox(operation.mailbox_id!, operation.result?.email)}
                          >
                            {t('查看邮箱', 'View mailbox')}
                            <ArrowUpRight size={13} />
                          </Button>
                        )}
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <Empty
                  icon={<History size={26} />}
                  title={t('还没有操作记录', 'No operations yet')}
                  description={t(
                    '邮箱创建与处理记录会显示在这里。',
                    'Mailbox requests and their results will appear here.',
                  )}
                />
              )}
            </section>
            <section className="dashboard-card dashboard-sync" aria-labelledby="dashboard-sync-title">
              <div className="dashboard-card-heading">
                <div>
                  <h2 id="dashboard-sync-title">{t('收件概况', 'Mail sync overview')}</h2>
                  <p>{t('来自本地缓存的同步信息', 'Sync information from the local cache')}</p>
                </div>
                <ShieldCheck size={21} />
              </div>
              <dl>
                <div>
                  <dt>{t('最近成功同步', 'Last successful sync')}</dt>
                  <dd>{formatDate(data.last_synced_at, true)}</dd>
                </div>
                <div>
                  <dt>{t('有效邮箱同步异常', 'Active mailboxes with sync errors')}</dt>
                  <dd className={data.mailboxes.sync_errors ? 'dashboard-sync-warning' : ''}>
                    {t(`${data.mailboxes.sync_errors} 个`, `${data.mailboxes.sync_errors}`)}
                  </dd>
                </div>
                <div>
                  <dt>{t('已过期邮箱', 'Expired mailboxes')}</dt>
                  <dd>{t(`${data.mailboxes.expired} 个`, `${data.mailboxes.expired}`)}</dd>
                </div>
              </dl>
              <p className="dashboard-note">
                {t(
                  '邮箱到期后停止收发，历史邮件保留至手动清除。',
                  'Expired mailboxes stop sending and receiving. Message history remains until you clear it.',
                )}
              </p>
            </section>
          </div>
        </>
      )}
    </section>
  );
}
