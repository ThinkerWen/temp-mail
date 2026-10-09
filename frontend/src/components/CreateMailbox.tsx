import { Button, Label, ListBox, Modal, Select } from '@heroui/react';
import { Clock3, Info, MailPlus, RefreshCw, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useI18n } from '../preferences';
import { formatDuration, providerName } from '../state';
import type { CreatePayload, Draft, Provider } from '../types';
import { ErrorNotice } from './Common';

type CreateMailboxProps = {
  open: boolean;
  setOpen: (value: boolean) => void;
  providers: Provider[];
  draft: Draft | null;
  busy: boolean;
  error: unknown;
  submit: (payload: CreatePayload) => Promise<void>;
};

export default function CreateMailbox(props: CreateMailboxProps) {
  return (
    <Modal.Backdrop
      isOpen={props.open}
      onOpenChange={(value) => {
        if (!props.busy) props.setOpen(value);
      }}
    >
      <CreateMailboxContent {...props} />
    </Modal.Backdrop>
  );
}

// Modal owns this content's lifetime: keep it through the exit animation, then reset the form.
function CreateMailboxContent({ setOpen, providers, draft, busy, error, submit }: CreateMailboxProps) {
  const t = useI18n();
  const [provider, setProvider] = useState('auto');
  const [ttl, setTtl] = useState('3600');
  const available = providers.filter((item) => item.capabilities.receive);
  const maximum =
    provider === 'auto'
      ? Math.max(0, ...available.map((item) => item.capabilities.max_ttl_seconds))
      : (available.find((item) => item.id === provider)?.capabilities.max_ttl_seconds ?? 0);
  const durations = [
    { value: 600, label: t('10 分钟', '10 minutes') },
    { value: 1800, label: t('30 分钟', '30 minutes') },
    { value: 3600, label: t('1 小时', '1 hour') },
    { value: 21600, label: t('6 小时', '6 hours') },
    { value: 86400, label: t('24 小时', '24 hours') },
  ].filter((item) => item.value <= maximum);
  if (maximum >= 60 && !durations.some((item) => item.value === maximum))
    durations.push({
      value: maximum,
      label: t(`${formatDuration(maximum)}（最长）`, `${formatDuration(maximum)} (maximum)`),
    });
  durations.sort((a, b) => a.value - b.value);
  useEffect(() => {
    if (draft) {
      setProvider(draft.payload.provider);
      setTtl(String(draft.payload.ttl_seconds));
    }
  }, [draft]);
  useEffect(() => {
    if (!draft && Number(ttl) > maximum && maximum >= 60) setTtl(String(Math.min(3600, maximum)));
  }, [maximum, ttl, draft]);
  return (
    <Modal.Container size="sm">
      <Modal.Dialog className="create-dialog">
        {!busy && <Modal.CloseTrigger aria-label={t('关闭创建窗口', 'Close create mailbox dialog')} />}
        <Modal.Header>
          <div className="dialog-icon">
            <MailPlus size={24} />
          </div>
          <Modal.Heading>{t('新建邮箱', 'New mailbox')}</Modal.Heading>
          <p>{t('一个临时地址，开始新的收件。', 'A temporary address for your next messages.')}</p>
        </Modal.Header>
        <Modal.Body>
          <form
            id="create-mailbox"
            onSubmit={(event) => {
              event.preventDefault();
              void submit({ provider, ttl_seconds: Number(ttl), required_capabilities: ['receive'] });
            }}
            className="create-form"
          >
            {draft && (
              <div className="notice">
                <Info size={17} />
                <span>
                  {t(
                    '上次请求尚未确认。继续提交会复用同一请求，不会重复创建。',
                    'The previous request is still unconfirmed. Continuing reuses that request without creating a duplicate mailbox.',
                  )}
                </span>
              </div>
            )}
            <Select
              value={provider}
              onChange={(value) => setProvider(String(value))}
              isDisabled={busy || !!draft}
              fullWidth
            >
              <Label>{t('邮箱供应商', 'Mailbox provider')}</Label>
              <Select.Trigger>
                <Select.Value />
                <Select.Indicator />
              </Select.Trigger>
              <Select.Popover>
                <ListBox>
                  <ListBox.Item id="auto" textValue={t('自动选择', 'Automatic selection')}>
                    <Sparkles size={16} />
                    {t('自动选择', 'Automatic selection')}
                    <ListBox.ItemIndicator />
                  </ListBox.Item>
                  {available.map((item) => (
                    <ListBox.Item key={item.id} id={item.id} textValue={providerName(item.id)}>
                      {providerName(item.id)}
                      <ListBox.ItemIndicator />
                    </ListBox.Item>
                  ))}
                </ListBox>
              </Select.Popover>
            </Select>
            <Select
              value={ttl}
              onChange={(value) => setTtl(String(value))}
              isDisabled={busy || !!draft}
              fullWidth
            >
              <Label>{t('有效期', 'Lifetime')}</Label>
              <Select.Trigger>
                <Clock3 size={16} />
                <Select.Value />
                <Select.Indicator />
              </Select.Trigger>
              <Select.Popover>
                <ListBox>
                  {durations.map((item) => (
                    <ListBox.Item key={item.value} id={String(item.value)} textValue={item.label}>
                      {item.label}
                      <ListBox.ItemIndicator />
                    </ListBox.Item>
                  ))}
                </ListBox>
              </Select.Popover>
            </Select>
            <p className="field-hint">
              {t(
                '自动选择会匹配支持该有效期的供应商。上游可能提前停止服务，请及时读取邮件。',
                'Automatic selection finds a provider that supports this lifetime. The upstream service may end sooner, so read your messages promptly.',
              )}
            </p>
            {!available.length && (
              <ErrorNotice
                error={t(
                  '暂无可用的收件供应商，请检查服务配置。',
                  'No receiving providers are available. Check the service configuration.',
                )}
              />
            )}
            <ErrorNotice error={error} />
          </form>
        </Modal.Body>
        <Modal.Footer>
          <Button variant="tertiary" isDisabled={busy} onPress={() => setOpen(false)}>
            {t('取消', 'Cancel')}
          </Button>
          <Button
            form="create-mailbox"
            type="submit"
            className="primary-action"
            isDisabled={busy || (!draft && (!durations.length || !available.length))}
          >
            {busy ? <RefreshCw size={16} className="spin" /> : <MailPlus size={16} />}
            {busy
              ? t('正在提交…', 'Submitting…')
              : draft
                ? t('继续上次创建', 'Continue previous request')
                : t('确认创建', 'Create mailbox')}
          </Button>
        </Modal.Footer>
      </Modal.Dialog>
    </Modal.Container>
  );
}
