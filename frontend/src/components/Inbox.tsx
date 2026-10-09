import { Button, Chip, Input, Label, TextField } from '@heroui/react';
import { ArrowLeft, Clock3, FileText, Mail, MailOpen, Plus, Search, ShieldCheck } from 'lucide-react';
import type { FormEvent } from 'react';
import { ApiError } from '../api';
import { MAILBOX_PAGE_SIZE, MESSAGE_PAGE_SIZE } from '../pagination';
import { useI18n } from '../preferences';
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
  const t = useI18n();
  const items = mailboxes.data?.items ?? [];
  const expired =
    selectedMailbox &&
    selectedMailbox.status !== 'deleted' &&
    (selectedMailbox.status === 'expired' || Date.parse(selectedMailbox.expires_at) <= now);
  const unavailable =
    selectedMailbox?.status === 'deleted' ||
    (messages.error instanceof ApiError && [404, 410].includes(messages.error.status));
  return (
    <div className={`inbox-workspace ${selectedMessage ? 'reading' : ''}`}>
      <section className="mailbox-panel" aria-label={t('邮箱列表', 'Mailbox list')}>
        <div className="panel-heading">
          <h2>
            {t('我的邮箱', 'My mailboxes')} <span className="count">{items.length}</span>
          </h2>
          <Button
            size="sm"
            variant="tertiary"
            isIconOnly
            aria-label={t('添加邮箱', 'Add mailbox')}
            onPress={create}
          >
            <Plus size={17} />
          </Button>
        </div>
        <form onSubmit={search} className="mailbox-search">
          <TextField value={query} onChange={setQuery}>
            <Label className="sr-only">{t('搜索邮箱', 'Search mailboxes')}</Label>
            <div className="search-input">
              <Search size={16} />
              <Input
                placeholder={t('输入完整邮箱地址，回车搜索', 'Enter a full email address, then press Enter')}
                enterKeyHint="search"
              />
            </div>
          </TextField>
        </form>
        <div className="list-caption">
          <span>{t('邮箱地址', 'Email address')}</span>
          <span>{t(`本页 ${items.length} 个`, `${items.length} on this page`)}</span>
        </div>
        <ErrorNotice error={mailboxes.error} retry={mailboxes.refresh} />
        <div className="mailbox-list">
          {!mailboxes.data && mailboxes.loading ? (
            <Loading />
          ) : !items.length ? (
            <Empty
              title={query ? t('没有找到邮箱', 'No matching mailboxes') : t('还没有邮箱', 'No mailboxes yet')}
              description={
                query
                  ? t(
                      '按完整地址查询，或清空搜索查看全部邮箱。',
                      'Search by full address, or clear the search to see all mailboxes.',
                    )
                  : t(
                      '创建一个临时地址，开始接收邮件。',
                      'Create a temporary address to start receiving mail.',
                    )
              }
              action={
                !query && (
                  <Button size="sm" variant="secondary" onPress={create}>
                    <Plus size={15} />
                    {t('新建邮箱', 'New mailbox')}
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
                      {active
                        ? t('使用中', 'Active')
                        : mailbox.status === 'deleted'
                          ? t('已删除', 'Deleted')
                          : t('已过期', 'Expired')}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>
        <Pager
          offset={mailboxOffset}
          pageSize={MAILBOX_PAGE_SIZE}
          count={items.length}
          total={mailboxes.data?.total}
          loading={mailboxes.loading}
          label={t('邮箱', 'mailboxes')}
          change={setMailboxOffset}
        />
        <div className="panel-footnote">
          <ShieldCheck size={14} />
          {t('每个邮箱绑定独立的供应商', 'Each mailbox stays with its original provider')}
        </div>
      </section>
      <section className="messages-panel" aria-label={t('邮件列表', 'Message list')}>
        <div className="panel-heading">
          <h2>{t('收件箱', 'Inbox')}</h2>
          <Chip size="sm" variant="soft" className="neutral-chip">
            {t(
              `${messages.data?.items.length ?? 0} 封 / 本页`,
              `${messages.data?.items.length ?? 0} ${(messages.data?.items.length ?? 0) === 1 ? 'message' : 'messages'} / page`,
            )}
          </Chip>
        </div>
        {selectedMailbox ? (
          <>
            <div className="selected-address">
              <div>
                <span className="section-kicker">{t('当前邮箱', 'Selected mailbox')}</span>
                <strong title={selectedMailbox.email}>{selectedMailbox.email}</strong>
              </div>
              <CopyButton value={selectedMailbox.email} />
            </div>
            <div className={`expiry-line ${expired || unavailable ? 'expired' : ''}`}>
              <Clock3 size={13} />
              <span>
                {selectedMailbox.status === 'deleted'
                  ? t('已删除', 'Deleted')
                  : expired
                    ? t('已过期', 'Expired')
                    : remaining(selectedMailbox.expires_at, now)}
              </span>
            </div>
            {expired && !unavailable && (
              <div className="notice" role="status">
                <Clock3 size={17} />
                <span>
                  {t(
                    '邮箱已过期，已停止收发，历史邮件仍可查看。',
                    'This mailbox has expired and no longer sends or receives mail. Its message history is still available.',
                  )}
                </span>
              </div>
            )}
            {!!selectedMailbox.last_sync_error_code && !expired && !unavailable && (
              <ErrorNotice
                error={t(
                  `收件同步暂未成功（${selectedMailbox.last_sync_error_code}），后台将继续尝试。`,
                  `Mail sync failed (${selectedMailbox.last_sync_error_code}). The worker will keep trying.`,
                )}
              />
            )}
            {unavailable ? (
              <Empty
                icon={<Clock3 size={27} />}
                title={t('邮箱已不可用', 'Mailbox unavailable')}
                description={t(
                  '这个邮箱已被删除或无法找到，请刷新邮箱列表。你也可以创建一个新邮箱。',
                  'This mailbox was deleted or could not be found. Refresh the mailbox list or create a new mailbox.',
                )}
                action={
                  <Button size="sm" variant="secondary" onPress={create}>
                    {t('创建新邮箱', 'Create a new mailbox')}
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
                      title={
                        expired
                          ? t('没有历史邮件', 'No message history')
                          : t('等待第一封来信', 'Waiting for your first message')
                      }
                      description={
                        expired
                          ? t(
                              '这个邮箱没有已保存的邮件。需要继续收件时，请创建一个新邮箱。',
                              'No messages were saved for this mailbox. Create a new mailbox to receive more mail.',
                            )
                          : t(
                              '复制邮箱地址并发送邮件，新的来信会自动出现在这里。',
                              'Copy this address and send it a message. New mail will appear here automatically.',
                            )
                      }
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
                        <h3>{item.subject || t('（无主题）', '(No subject)')}</h3>
                        <div className="message-row-bottom">
                          <span>
                            <FileText size={12} />
                            {t('纯文本邮件', 'Plain text message')}
                          </span>
                          <time dateTime={item.received_at}>{formatDate(item.received_at, true)}</time>
                        </div>
                      </button>
                    ))
                  )}
                </div>
                <Pager
                  offset={messageOffset}
                  pageSize={MESSAGE_PAGE_SIZE}
                  count={messages.data?.items.length ?? 0}
                  loading={messages.loading}
                  label={t('邮件', 'messages')}
                  change={setMessageOffset}
                />
                <div className="panel-footnote">
                  <span className="tiny-dot" />
                  {t('上次收件同步：', 'Last mail sync: ')}
                  {formatDate(messages.data?.last_synced_at ?? selectedMailbox.last_synced_at, true)}
                </div>
              </>
            )}
          </>
        ) : (
          <Empty
            title={t('选择一个邮箱', 'Select a mailbox')}
            description={t(
              '从左侧选择邮箱，查看它的收件箱。',
              'Select a mailbox on the left to view its inbox.',
            )}
          />
        )}
      </section>
      <section className="reader-panel" aria-label={t('邮件正文', 'Message body')}>
        <div className="reader-toolbar">
          <Button className="reader-back" size="sm" variant="tertiary" onPress={() => selectMessage(null)}>
            <ArrowLeft size={15} />
            {t('返回收件箱', 'Back to inbox')}
          </Button>
          <span>
            <MailOpen size={16} />
            {t('邮件阅读', 'Message reader')}
          </span>
          {message.data && !unavailable && (
            <CopyButton label={t('复制邮件正文', 'Copy message body')} value={message.data.text} />
          )}
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
            <h3>{t('让消息，在这里展开', 'Your messages, opened up')}</h3>
            <p>
              {t('选择一封邮件，阅读完整内容。', 'Select a message to read it in full.')}
              <br />
              {t('简单、专注，不错过每一封来信。', 'A simple, focused space for every message.')}
            </p>
            <span className="reader-hint">
              <ShieldCheck size={13} />
              {t('纯文本阅读，不加载外部图片', 'Plain text only. No external images loaded.')}
            </span>
          </div>
        ) : message.error ? (
          <ErrorNotice error={message.error} retry={message.refresh} />
        ) : !message.data ? (
          <Loading label={t('正在打开邮件…', 'Opening message…')} />
        ) : (
          <article className="message-detail">
            <span className="section-kicker">INCOMING MESSAGE</span>
            <h2>{message.data.subject || t('（无主题）', '(No subject)')}</h2>
            <div className="message-envelope">
              <span className="sender-avatar large">{message.data.sender.slice(0, 1).toUpperCase()}</span>
              <div>
                <strong>{message.data.sender}</strong>
                <p>
                  {t('发送至', 'To')} <span>{message.data.recipients.join(', ')}</span>
                </p>
                <time dateTime={message.data.received_at}>{formatDate(message.data.received_at)}</time>
              </div>
            </div>
            <div className="message-body" data-testid="message-body">
              {message.data.text || t('（这封邮件没有正文）', '(This message has no body)')}
            </div>
            <footer>
              <ShieldCheck size={13} />
              {t('邮件以纯文本展示', 'Messages are displayed as plain text')}
            </footer>
          </article>
        )}
      </section>
    </div>
  );
}
