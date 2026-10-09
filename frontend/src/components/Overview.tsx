import { Button, Card, Chip, Input, Label, TextField } from '@heroui/react';
import { ArrowUpRight, Check, Clock3, Globe2, History, Layers, RefreshCw, Search } from 'lucide-react';
import { useState } from 'react';
import { errorMessage } from '../api';
import { formatDate, providerName } from '../state';
import type { Operation, Provider } from '../types';
import { CopyButton, Empty, ErrorNotice, Loading, OperationBadge } from './Common';

export function Providers({ providers }: { providers: Provider[] }) {
  return (
    <div className="providers-view">
      <div className="section-intro">
        <div>
          <h2>每个地址，都有自己的归属</h2>
          <p>创建时选择供应商，后续收件自动回到同一个来源。</p>
        </div>
        <Layers size={27} strokeWidth={1.3} />
      </div>
      <div className="provider-grid">
        {providers.map((item, index) => (
          <Card key={item.id} className="provider-card">
            <Card.Header>
              <div className={`provider-logo provider-${index % 3}`}>
                <Globe2 size={25} />
              </div>
              <div>
                <Card.Title>{providerName(item.id)}</Card.Title>
                <Card.Description>{item.id}</Card.Description>
              </div>
              <Chip size="sm" variant="soft" className="neutral-chip">
                已配置
              </Chip>
            </Card.Header>
            <Card.Content>
              <div className="provider-capabilities">
                {[
                  ['创建邮箱', true],
                  ['接收邮件', item.capabilities.receive],
                  ['发送邮件', item.capabilities.send],
                  ['附件', item.capabilities.attachments],
                ].map(([label, supported]) => (
                  <span key={String(label)} className={supported ? '' : 'unsupported'}>
                    {supported ? <Check size={14} /> : <span className="dash">—</span>}
                    {label}
                  </span>
                ))}
              </div>
              <div className="provider-limit">
                <Clock3 size={15} />
                <span>最长本地有效期</span>
                <strong>
                  {item.capabilities.max_ttl_seconds / 3600 >= 1
                    ? `${item.capabilities.max_ttl_seconds / 3600} 小时`
                    : `${item.capabilities.max_ttl_seconds / 60} 分钟`}
                </strong>
              </div>
            </Card.Content>
            <Card.Footer>
              <span>按配置顺序 #{index + 1}</span>
              <span>
                固定绑定 <ArrowUpRight size={13} />
              </span>
            </Card.Footer>
          </Card>
        ))}
      </div>
      {!providers.length && (
        <Empty title="尚未配置供应商" description="请在服务配置中启用至少一个邮箱供应商。" />
      )}
      <p className="page-note">
        这里显示服务已加载的能力配置，不代表供应商当前在线。实际可用性以创建和收件结果为准。
      </p>
    </div>
  );
}

export function Operations({
  operations,
  error,
  loading,
  retry,
  addOperation,
  openMailbox,
}: {
  operations: Operation[];
  error: unknown;
  loading: boolean;
  retry: () => void;
  addOperation: (id: string) => Promise<void>;
  openMailbox: (id: string, email?: string) => void;
}) {
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
          <h2>每次创建，都有迹可循</h2>
          <p>显示本标签页跟踪的操作。已有操作也可以通过 ID 找回。</p>
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
          <Label className="sr-only">操作 ID</Label>
          <Input placeholder="输入 op_ 开头的操作 ID" />
        </TextField>
        <Button type="submit" variant="secondary" isDisabled={looking || !id.trim()}>
          {looking ? <RefreshCw size={15} className="spin" /> : <Search size={15} />}查询操作
        </Button>
      </form>
      <ErrorNotice error={lookupError} />
      <ErrorNotice error={error} retry={retry} />
      {!operations.length ? (
        loading ? (
          <Loading />
        ) : (
          <Empty
            icon={<History size={27} />}
            title="还没有操作记录"
            description="创建邮箱后，可以在这里查看处理状态和结果。"
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
                    {item.kind === 'create' ? '创建邮箱' : item.kind === 'send' ? '发送邮件' : '删除邮箱'}
                  </h3>
                  <OperationBadge status={item.status} />
                </div>
                <div className="operation-id">
                  <code>{item.id}</code>
                  <CopyButton value={item.id} label="复制操作 ID" />
                </div>
                <p>
                  {providerName(item.provider_id)}
                  <span>·</span>
                  {formatDate(item.created_at, true)}
                </p>
                {item.status === 'pending' && (
                  <p className="operation-note">正在等待后台处理；长时间未变化时，请检查 Worker 是否运行。</p>
                )}
                {item.status === 'running' && <p className="operation-note">后台正在联系供应商，请稍候。</p>}
                {item.status === 'unknown' && (
                  <p className="operation-warning">
                    结果尚不能确认，不会自动重新创建。可保留操作 ID 并检查后台日志。
                  </p>
                )}
                {item.error_code && <p className="operation-warning">{item.error_code}</p>}
                {item.result?.email && <p className="operation-email">{item.result.email}</p>}
              </div>
              {item.status === 'succeeded' && (item.mailbox_id || item.result?.mailbox_id) && (
                <Button
                  size="sm"
                  variant="secondary"
                  onPress={() =>
                    openMailbox((item.mailbox_id ?? item.result?.mailbox_id)!, item.result?.email)
                  }
                >
                  查看邮箱
                  <ArrowUpRight size={14} />
                </Button>
              )}
            </article>
          ))}
        </div>
      )}
      {!!error && <p className="page-note">{errorMessage(error)}</p>}
    </div>
  );
}
