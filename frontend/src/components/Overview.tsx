import { Button, Input, Label, TextField } from '@heroui/react';
import { ArrowUpRight, History, RefreshCw, Search } from 'lucide-react';
import { useState } from 'react';
import { errorMessage } from '../api';
import { OPERATION_PAGE_SIZE } from '../pagination';
import { useI18n } from '../preferences';
import { formatDate, providerName } from '../state';
import type { Operation } from '../types';
import { CopyButton, Empty, ErrorNotice, Loading, OperationBadge, Pager } from './Common';

export function Operations({
  operations,
  error,
  loading,
  retry,
  addOperation,
  openMailbox,
  offset,
  total,
  changePage,
  filtered,
  clearFilter,
}: {
  operations: Operation[];
  error: unknown;
  loading: boolean;
  retry: () => void;
  addOperation: (id: string) => Promise<void>;
  openMailbox: (id: string, email?: string) => void;
  offset: number;
  total: number;
  changePage: (offset: number) => void;
  filtered: boolean;
  clearFilter: () => void;
}) {
  const t = useI18n();
  const [id, setId] = useState('');
  const [lookupError, setLookupError] = useState<unknown>(null);
  const [looking, setLooking] = useState(false);
  async function lookup() {
    if (!id.trim() || looking) return;
    setLooking(true);
    setLookupError(null);
    try {
      await addOperation(id.trim());
      setId('');
    } catch (cause) {
      setLookupError(cause);
    } finally {
      setLooking(false);
    }
  }
  return (
    <div className="operations-view">
      <div className="section-intro">
        <div>
          <h2>{t('每次操作，都有迹可循', 'Keep track of every operation')}</h2>
          <p>
            {t(
              '历史记录按时间倒序展示，重新登录或重启服务后仍可查看。',
              'History is shown newest first and remains available after signing in again or restarting the service.',
            )}
          </p>
        </div>
        <History size={27} strokeWidth={1.3} />
      </div>
      <form
        className="operation-search"
        onSubmit={(event) => {
          event.preventDefault();
          void lookup();
        }}
      >
        <TextField value={id} onChange={setId}>
          <Label className="sr-only">{t('操作 ID', 'Operation ID')}</Label>
          <Input placeholder={t('输入 op_ 开头的操作 ID', 'Enter an operation ID starting with op_')} />
        </TextField>
        <Button type="submit" variant="secondary" isDisabled={looking || !id.trim()}>
          {looking ? <RefreshCw size={15} className="spin" /> : <Search size={15} />}
          {t('查询操作', 'Find operation')}
        </Button>
        {filtered && (
          <Button variant="tertiary" isDisabled={looking} onPress={clearFilter}>
            {t('返回全部记录', 'Back to all activity')}
          </Button>
        )}
      </form>
      <ErrorNotice error={lookupError} />
      <ErrorNotice error={error} retry={retry} />
      {!operations.length ? (
        loading ? (
          <Loading />
        ) : (
          <Empty
            icon={<History size={27} />}
            title={t('还没有操作记录', 'No operations yet')}
            description={t(
              '创建邮箱后，可以在这里查看处理状态和结果。',
              'After creating a mailbox, track its progress and result here.',
            )}
          />
        )
      ) : (
        <div className="operations-list">
          {operations.map((item) => (
            <article key={item.id} className="operation-row">
              <div className="operation-icon">
                <History size={20} />
              </div>
              <div className="operation-content">
                <div className="operation-title">
                  <h3>
                    {item.kind === 'create'
                      ? t('创建邮箱', 'Create mailbox')
                      : item.kind === 'send'
                        ? t('发送邮件', 'Send message')
                        : t('删除邮箱', 'Delete mailbox')}
                  </h3>
                  <OperationBadge status={item.status} />
                </div>
                <div className="operation-id">
                  <code>{item.id}</code>
                  <CopyButton value={item.id} label={t('复制操作 ID', 'Copy operation ID')} />
                </div>
                <p>
                  {providerName(item.provider_id)}
                  <span>·</span>
                  {formatDate(item.created_at, true)}
                </p>
                {item.status === 'pending' && (
                  <p className="operation-note">
                    {t(
                      '正在等待后台处理；长时间未变化时，请检查 Worker 是否运行。',
                      'Waiting for the worker. If this does not change, check that the worker is running.',
                    )}
                  </p>
                )}
                {item.status === 'running' && (
                  <p className="operation-note">
                    {t('后台正在联系供应商，请稍候。', 'The worker is contacting the provider. Please wait.')}
                  </p>
                )}
                {item.status === 'unknown' && (
                  <p className="operation-warning">
                    {t(
                      '结果尚不能确认，不会自动重新创建。可保留操作 ID 并检查后台日志。',
                      'The result is unconfirmed and creation will not be retried automatically. Keep the operation ID and check the service logs.',
                    )}
                  </p>
                )}
                {item.error_code && <p className="operation-warning">{item.error_code}</p>}
                {item.result?.email && <p className="operation-email">{item.result.email}</p>}
              </div>
              {item.status === 'succeeded' && item.mailbox_id && (
                <Button
                  size="sm"
                  variant="secondary"
                  onPress={() => openMailbox(item.mailbox_id!, item.result?.email)}
                >
                  {t('查看邮箱', 'View mailbox')}
                  <ArrowUpRight size={14} />
                </Button>
              )}
            </article>
          ))}
        </div>
      )}
      {!filtered && (
        <Pager
          offset={offset}
          pageSize={OPERATION_PAGE_SIZE}
          count={operations.length}
          total={total}
          loading={loading}
          label={t('操作记录', 'activity')}
          change={changePage}
        />
      )}
      {!!error && <p className="page-note">{errorMessage(error)}</p>}
    </div>
  );
}
