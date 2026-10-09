import { Button, Label, ListBox, Modal, Select } from '@heroui/react';
import { Clock3, Info, MailPlus, RefreshCw, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { providerName } from '../state';
import type { CreatePayload, Draft, Provider } from '../types';
import { ErrorNotice } from './Common';

export default function CreateMailbox({
  open,
  setOpen,
  providers,
  draft,
  busy,
  error,
  submit,
}: {
  open: boolean;
  setOpen: (value: boolean) => void;
  providers: Provider[];
  draft: Draft | null;
  busy: boolean;
  error: unknown;
  submit: (payload: CreatePayload) => Promise<void>;
}) {
  const [provider, setProvider] = useState('auto');
  const [ttl, setTtl] = useState('3600');
  const available = providers.filter((item) => item.capabilities.receive);
  const maximum =
    provider === 'auto'
      ? Math.max(0, ...available.map((item) => item.capabilities.max_ttl_seconds))
      : (available.find((item) => item.id === provider)?.capabilities.max_ttl_seconds ?? 0);
  const durations = [
    { value: 600, label: '10 分钟' },
    { value: 1800, label: '30 分钟' },
    { value: 3600, label: '1 小时' },
    { value: 21600, label: '6 小时' },
    { value: 86400, label: '24 小时' },
  ].filter((item) => item.value <= maximum);
  if (maximum >= 60 && !durations.some((item) => item.value === maximum))
    durations.push({ value: maximum, label: `${maximum / 60} 分钟（最长）` });
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
    <Modal.Backdrop
      isOpen={open}
      onOpenChange={(value) => {
        if (!busy) setOpen(value);
      }}
    >
      <Modal.Container size="sm">
        <Modal.Dialog className="create-dialog">
          {!busy && <Modal.CloseTrigger aria-label="关闭创建窗口" />}
          <Modal.Header>
            <div className="dialog-icon">
              <MailPlus size={24} />
            </div>
            <Modal.Heading>新建邮箱</Modal.Heading>
            <p>一个临时地址，开始新的收件。</p>
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
                  <span>上次请求尚未确认。继续提交会复用同一请求，不会重复创建。</span>
                </div>
              )}
              <Select
                value={provider}
                onChange={(value) => setProvider(String(value))}
                isDisabled={busy || !!draft}
                fullWidth
              >
                <Label>邮箱供应商</Label>
                <Select.Trigger>
                  <Select.Value />
                  <Select.Indicator />
                </Select.Trigger>
                <Select.Popover>
                  <ListBox>
                    <ListBox.Item id="auto" textValue="自动选择">
                      <Sparkles size={16} />
                      自动选择
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
                <Label>有效期</Label>
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
                自动选择会匹配支持该有效期的供应商。上游可能提前停止服务，请及时读取邮件。
              </p>
              {!available.length && <ErrorNotice error="暂无可用的收件供应商，请检查服务配置。" />}
              <ErrorNotice error={error} />
            </form>
          </Modal.Body>
          <Modal.Footer>
            <Button variant="tertiary" isDisabled={busy} onPress={() => setOpen(false)}>
              取消
            </Button>
            <Button
              form="create-mailbox"
              type="submit"
              className="primary-action"
              isDisabled={busy || (!draft && (!durations.length || !available.length))}
            >
              {busy ? <RefreshCw size={16} className="spin" /> : <MailPlus size={16} />}
              {busy ? '正在提交…' : draft ? '继续上次创建' : '确认创建'}
            </Button>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  );
}
