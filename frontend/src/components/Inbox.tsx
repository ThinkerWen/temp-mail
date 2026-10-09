import { Button, Chip, Input, Label, TextField } from '@heroui/react';
import {
  ArrowLeft,
  ArrowUpRight,
  Clock3,
  FileText,
  Mail,
  MailOpen,
  Plus,
  Search,
  ShieldCheck,
} from 'lucide-react';
import type { FormEvent } from 'react';
import { ApiError } from '../api';
import { formatDate, providerName, remaining } from '../state';
import type { Mailbox, Message, MessagePage, Page } from '../types';
import { CopyButton, Empty, ErrorNotice, Loading, Pager } from './Common';

type Resource<T> = { data?: T; error?: unknown; loading: boolean; refresh: () => void };
export default function Inbox({
  mailboxes,
  messages,
  message,
  selectedMailbox,
  selectedMessage,
  selectMailbox,
  selectMessage,
  mailboxOffset,
  messageOffset,
  setMailboxOffset,
  setMessageOffset,
  query,
  setQuery,
  search,
  now,
  create,
}: {
  mailboxes: Resource<Page<Mailbox>>;
  messages: Resource<MessagePage>;
  message: Resource<Message>;
  selectedMailbox: Mailbox | undefined;
  selectedMessage: string | null;
  selectMailbox: (id: string) => void;
  selectMessage: (id: string | null) => void;
  mailboxOffset: number;
  messageOffset: number;
  setMailboxOffset: (value: number) => void;
  setMessageOffset: (value: number) => void;
  query: string;
  setQuery: (value: string) => void;
  search: (event: FormEvent) => void;
  now: number;
  create: () => void;
}) {
  const items = mailboxes.data?.items ?? [];
  const expired =
    selectedMailbox && (selectedMailbox.status !== 'active' || Date.parse(selectedMailbox.expires_at) <= now);
  const unavailable =
    expired || (messages.error instanceof ApiError && [404, 410].includes(messages.error.status));
  return (
    <div className={`inbox-workspace ${selectedMessage ? 'reading' : ''}`}>
      <section className="mailbox-panel" aria-label="邮箱列表">
        <div className="panel-heading">
          <h2>
            我的邮箱 <span className="count">{items.length}</span>
          </h2>
          <Button size="sm" variant="tertiary" isIconOnly aria-label="添加邮箱" onPress={create}>
            <Plus size={17} />
          </Button>
        </div>
        <form onSubmit={search} className="mailbox-search">
          <TextField value={query} onChange={setQuery}>
            <Label className="sr-only">搜索邮箱</Label>
            <div className="search-input">
              <Search size={16} />
              <Input placeholder="输入完整邮箱地址" />
              <Button type="submit" size="sm" variant="tertiary" isIconOnly aria-label="搜索邮箱地址">
                <ArrowUpRight size={15} />
              </Button>
            </div>
          </TextField>
        </form>
        <div className="list-caption">
          <span>邮箱地址</span>
          <span>本页 {items.length} 个</span>
        </div>
        <ErrorNotice error={mailboxes.error} retry={mailboxes.refresh} />
        <div className="mailbox-list">
          {!mailboxes.data && mailboxes.loading ? (
            <Loading />
          ) : !items.length ? (
            <Empty
              title={query ? '没有找到邮箱' : '还没有邮箱'}
              description={
                query ? '按完整地址查询，或清空搜索查看全部邮箱。' : '创建一个临时地址，开始接收邮件。'
              }
              action={
                !query && (
                  <Button size="sm" variant="secondary" onPress={create}>
                    <Plus size={15} />
                    新建邮箱
                  </Button>
                )
              }
            />
          ) : (
            items.map((mailbox) => {
              const active = mailbox.status === 'active' && Date.parse(mailbox.expires_at) > now;
              const at = mailbox.email.lastIndexOf('@');
              return (
                <button
                  key={mailbox.id}
                  className={`mailbox-row ${selectedMailbox?.id === mailbox.id ? 'selected' : ''}`}
                  onClick={() => selectMailbox(mailbox.id)}
                  aria-pressed={selectedMailbox?.id === mailbox.id}
                  aria-label={mailbox.email}
                >
                  <span className={`mailbox-icon ${active ? '' : 'muted'}`}>
                    <Mail size={19} strokeWidth={1.6} />
                  </span>
                  <span className="mailbox-row-content">
                    <strong title={mailbox.email}>{mailbox.email.slice(0, at)}</strong>
                    <span className="mailbox-domain">{mailbox.email.slice(at)}</span>
                    <span className="mailbox-row-meta">
                      <span>{providerName(mailbox.provider_id)}</span>
                      <span className={active ? 'active-dot' : 'inactive-dot'} />
                      {active ? '使用中' : mailbox.status === 'deleted' ? '已删除' : '已过期'}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>
        <Pager
          offset={mailboxOffset}
          count={items.length}
          loading={mailboxes.loading}
          label="邮箱"
          change={setMailboxOffset}
        />
        <div className="panel-footnote">
          <ShieldCheck size={14} />
          每个邮箱绑定独立的供应商
        </div>
      </section>
      <section className="messages-panel" aria-label="邮件列表">
        <div className="panel-heading">
          <h2>收件箱</h2>
          <Chip size="sm" variant="soft" className="neutral-chip">
            {messages.data?.items.length ?? 0} 封 / 本页
          </Chip>
        </div>
        {selectedMailbox ? (
          <>
            <div className="selected-address">
              <div>
                <span className="section-kicker">当前邮箱</span>
                <strong title={selectedMailbox.email}>{selectedMailbox.email}</strong>
              </div>
              <CopyButton value={selectedMailbox.email} />
            </div>
            <div className={`expiry-line ${expired ? 'expired' : ''}`}>
              <Clock3 size={13} />
              <span>{remaining(selectedMailbox.expires_at, now)}</span>
            </div>
            {!!selectedMailbox.last_sync_error_code && !expired && (
              <ErrorNotice
                error={`收件同步暂未成功（${selectedMailbox.last_sync_error_code}），后台将继续尝试。`}
              />
            )}
            {unavailable ? (
              <Empty
                icon={<Clock3 size={27} />}
                title="邮箱已不可用"
                description="临时地址已结束服务，邮件内容不再保留。你可以创建一个新邮箱。"
                action={
                  <Button size="sm" variant="secondary" onPress={create}>
                    创建新邮箱
                  </Button>
                }
              />
            ) : (
              <>
                <ErrorNotice error={messages.error} retry={messages.refresh} />
                <div className="message-list">
                  {!messages.data && messages.loading ? (
                    <Loading />
                  ) : !messages.data?.items.length ? (
                    <Empty
                      title="等待第一封来信"
                      description="复制邮箱地址并发送邮件，新的来信会自动出现在这里。"
                    />
                  ) : (
                    messages.data.items.map((item) => (
                      <button
                        key={item.id}
                        onClick={() => selectMessage(item.id)}
                        className={`message-row ${item.id === selectedMessage ? 'selected' : ''}`}
                        aria-pressed={item.id === selectedMessage}
                      >
                        <div className="message-row-top">
                          <span className="sender-avatar">
                            {item.sender.replace(/^"/, '').slice(0, 1).toUpperCase()}
                          </span>
                          <strong title={item.sender}>{item.sender}</strong>
                        </div>
                        <h3>{item.subject || '（无主题）'}</h3>
                        <div className="message-row-bottom">
                          <span>
                            <FileText size={12} />
                            纯文本邮件
                          </span>
                          <time dateTime={item.received_at}>{formatDate(item.received_at, true)}</time>
                        </div>
                      </button>
                    ))
                  )}
                </div>
                <Pager
                  offset={messageOffset}
                  count={messages.data?.items.length ?? 0}
                  loading={messages.loading}
                  label="邮件"
                  change={setMessageOffset}
                />
                <div className="panel-footnote">
                  <span className="tiny-dot" />
                  上次收件同步：
                  {formatDate(messages.data?.last_synced_at ?? selectedMailbox.last_synced_at, true)}
                </div>
              </>
            )}
          </>
        ) : (
          <Empty title="选择一个邮箱" description="从左侧选择邮箱，查看它的收件箱。" />
        )}
      </section>
      <section className="reader-panel" aria-label="邮件正文">
        <div className="reader-toolbar">
          <Button className="reader-back" size="sm" variant="tertiary" onPress={() => selectMessage(null)}>
            <ArrowLeft size={15} />
            返回收件箱
          </Button>
          <span>
            <MailOpen size={16} />
            邮件阅读
          </span>
          {message.data && !unavailable && <CopyButton label="复制邮件正文" value={message.data.text} />}
        </div>
        {unavailable || !selectedMessage ? (
          <div className="reader-empty">
            <div className="reader-art" aria-hidden="true">
              <div className="art-circle" />
              <div className="art-paper">
                <span />
                <span />
                <span />
              </div>
              <MailOpen size={65} strokeWidth={1.05} />
              <span className="art-spark">✦</span>
            </div>
            <h3>让消息，在这里展开</h3>
            <p>
              选择一封邮件，阅读完整内容。
              <br />
              简单、专注，不错过每一封来信。
            </p>
            <span className="reader-hint">
              <ShieldCheck size={13} />
              纯文本阅读，不加载外部图片
            </span>
          </div>
        ) : message.error ? (
          <ErrorNotice error={message.error} retry={message.refresh} />
        ) : !message.data ? (
          <Loading label="正在打开邮件…" />
        ) : (
          <article className="message-detail">
            <span className="section-kicker">INCOMING MESSAGE</span>
            <h2>{message.data.subject || '（无主题）'}</h2>
            <div className="message-envelope">
              <span className="sender-avatar large">{message.data.sender.slice(0, 1).toUpperCase()}</span>
              <div>
                <strong>{message.data.sender}</strong>
                <p>
                  发送至 <span>{message.data.recipients.join(', ')}</span>
                </p>
                <time dateTime={message.data.received_at}>{formatDate(message.data.received_at)}</time>
              </div>
            </div>
            <div className="message-body" data-testid="message-body">
              {message.data.text || '（这封邮件没有正文）'}
            </div>
            <footer>
              <ShieldCheck size={13} />
              邮件以纯文本展示
            </footer>
          </article>
        )}
      </section>
    </div>
  );
}
