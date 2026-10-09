import { Button, Card, Chip, Label, ListBox, Modal, Select, Switch } from '@heroui/react';
import { Check, Clock3, Globe2, Info, Layers, Pencil, RefreshCw, Save } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, restartRequiredMessage } from '../api';
import { useI18n } from '../preferences';
import { formatDuration, providerName } from '../state';
import type { Configuration, ConfigurationPatch, ProviderConfig } from '../types';
import { Empty, ErrorNotice, Loading } from './Common';
import { ConfigField } from './ConfigEditor';

type ProviderDraft = {
  id: string;
  index_url: string;
  base_url: string;
  timeout_seconds: string;
  impersonate: string;
  max_ttl_seconds: string;
  proxy: string;
  clearProxy: boolean;
  priority: string;
};

function makeDraft(provider: ProviderConfig, index: number): ProviderDraft {
  return {
    id: provider.id,
    index_url: provider.index_url ?? '',
    base_url: provider.base_url,
    timeout_seconds: String(provider.timeout_seconds),
    impersonate: provider.impersonate,
    max_ttl_seconds: String(provider.max_ttl_seconds),
    proxy: '',
    clearProxy: false,
    priority: String(index),
  };
}

function providerPayload(provider: ProviderConfig) {
  return {
    id: provider.id,
    enabled: provider.enabled,
    index_url: provider.index_url,
    base_url: provider.base_url,
    timeout_seconds: provider.timeout_seconds,
    impersonate: provider.impersonate,
    max_ttl_seconds: provider.max_ttl_seconds,
  };
}

export default function Providers({
  token,
  disconnect,
  onSaved,
}: {
  token: string;
  disconnect: (message?: string) => void;
  onSaved: (config: Configuration) => void;
}) {
  const t = useI18n();
  const [config, setConfig] = useState<Configuration | null>(null);
  const [draft, setDraft] = useState<ProviderDraft | null>(null);
  const [baseline, setBaseline] = useState('');
  const [loading, setLoading] = useState(true);
  const [reloadAnimating, setReloadAnimating] = useState(false);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<{ id: string } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const dirty = !!draft && JSON.stringify(draft) !== baseline;
  const busy = loading || savingId !== null;
  const editing = config?.providers.find((provider) => provider.id === draft?.id);

  useEffect(() => {
    if (!reloadAnimating) return;
    const timer = setTimeout(() => setReloadAnimating(false), 650);
    return () => clearTimeout(timer);
  }, [reloadAnimating]);

  function report(cause: unknown) {
    if (cause instanceof ApiError && cause.status === 401) disconnect(cause.message);
    else setError(cause);
  }

  useEffect(() => {
    const request = new AbortController();
    controller.current = request;
    api
      .configuration(token, request.signal)
      .then((value) => {
        if (!request.signal.aborted) setConfig(value);
      })
      .catch((cause: unknown) => {
        if (!request.signal.aborted) {
          if (cause instanceof ApiError && cause.status === 401) disconnect(cause.message);
          else setError(cause);
        }
      })
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
    return () => controller.current?.abort();
  }, [token, disconnect]);

  useEffect(() => {
    if (!dirty && savingId === null) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    const beforeNavigate = (event: Event) => {
      if (
        savingId !== null ||
        !window.confirm(
          t(
            '配置尚未保存。离开页面会丢失修改，是否继续？',
            'You have unsaved configuration changes. Leave and discard them?',
          ),
        )
      )
        event.preventDefault();
    };
    window.addEventListener('beforeunload', beforeUnload);
    window.addEventListener('temp-mail:navigate', beforeNavigate);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      window.removeEventListener('temp-mail:navigate', beforeNavigate);
    };
  }, [dirty, savingId, t]);

  function openEditor(provider: ProviderConfig, index: number) {
    if (busy) return;
    const next = makeDraft(provider, index);
    setDraft(next);
    setBaseline(JSON.stringify(next));
    setError(null);
  }

  function closeEditor() {
    if (busy || inFlight.current) return;
    if (
      dirty &&
      !window.confirm(
        t(
          '配置尚未保存。关闭会丢失当前修改，是否继续？',
          'You have unsaved configuration changes. Close and discard them?',
        ),
      )
    )
      return;
    setDraft(null);
    setBaseline('');
    setError(null);
  }

  async function reload() {
    if (inFlight.current || busy || reloadAnimating) return;
    if (
      dirty &&
      !window.confirm(
        t(
          '重新读取配置会丢失当前未保存的修改，是否继续？',
          'Reloading will discard your unsaved changes. Continue?',
        ),
      )
    )
      return;
    inFlight.current = true;
    const request = new AbortController();
    controller.current = request;
    setReloadAnimating(true);
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const value = await api.configuration(token, request.signal);
      if (!request.signal.aborted) {
        setConfig(value);
        if (draft) {
          const index = value.providers.findIndex((provider) => provider.id === draft.id);
          if (index >= 0) {
            const next = makeDraft(value.providers[index]!, index);
            setDraft(next);
            setBaseline(JSON.stringify(next));
          } else setDraft(null);
        }
      }
    } catch (cause) {
      if (!request.signal.aborted) report(cause);
    } finally {
      inFlight.current = false;
      if (!request.signal.aborted) setLoading(false);
    }
  }

  async function persist(
    id: string,
    providers: NonNullable<ConfigurationPatch['providers']>,
    close: boolean,
  ) {
    if (!config || busy || inFlight.current) return;
    inFlight.current = true;
    const request = new AbortController();
    controller.current = request;
    setSavingId(id);
    setError(null);
    setMessage(null);
    try {
      const value = await api.saveConfiguration(
        token,
        { revision: config.revision, providers },
        request.signal,
      );
      if (!request.signal.aborted) {
        setConfig(value);
        setMessage({ id });
        onSaved(value);
        if (close) {
          setDraft(null);
          setBaseline('');
        }
      }
    } catch (cause) {
      if (!request.signal.aborted) report(cause);
    } finally {
      inFlight.current = false;
      if (!request.signal.aborted) setSavingId(null);
    }
  }

  function toggle(provider: ProviderConfig, enabled: boolean) {
    if (!config || busy || draft) return;
    const providers = config.providers.map((item) => ({
      ...providerPayload(item),
      enabled: item.id === provider.id ? enabled : item.enabled,
    }));
    void persist(provider.id, providers, false);
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!config || !draft || !dirty || busy) return;
    const providers: NonNullable<ConfigurationPatch['providers']> = config.providers.map((item) =>
      item.id === draft.id
        ? {
            ...providerPayload(item),
            index_url: draft.index_url,
            base_url: draft.base_url,
            timeout_seconds: Number(draft.timeout_seconds),
            impersonate: draft.impersonate,
            max_ttl_seconds: Number(draft.max_ttl_seconds),
            ...(draft.clearProxy ? { proxy: null } : draft.proxy ? { proxy: draft.proxy } : {}),
          }
        : providerPayload(item),
    );
    const index = providers.findIndex((item) => item.id === draft.id);
    const [selected] = providers.splice(index, 1);
    providers.splice(Number(draft.priority), 0, selected!);
    void persist(draft.id, providers, true);
  }

  function edit(patch: Partial<ProviderDraft>) {
    setDraft((current) => current && { ...current, ...patch });
  }

  return (
    <div className="providers-view">
      <div className="section-intro">
        <div>
          <h2>{t('已支持的供应商', 'Supported providers')}</h2>
          <p>
            {t(
              '按需开启服务，编辑连接参数与邮箱有效期。',
              'Enable providers and edit their connection settings and mailbox lifetimes.',
            )}
          </p>
        </div>
        <Button
          size="sm"
          variant="tertiary"
          isDisabled={busy || reloadAnimating || !!draft}
          aria-busy={loading || reloadAnimating}
          onPress={() => void reload()}
        >
          <RefreshCw size={15} className={loading || reloadAnimating ? 'spin' : ''} />
          {t('重新读取配置', 'Reload configuration')}
        </Button>
      </div>
      {config?.restart_required && (
        <div className="notice config-restart" role="status">
          <Info size={18} />
          <span>{restartRequiredMessage(config)}</span>
        </div>
      )}
      {message && (
        <div className="notice provider-saved" role="status">
          <Check size={17} />
          <span>
            {t(
              `${providerName(message.id)} 的配置已保存并在线应用。Worker 会在当前任务结束后自动采用新配置。`,
              `Configuration saved and applied for ${providerName(message.id)}. The Worker adopts changes after its current task finishes.`,
            )}
          </span>
        </div>
      )}
      {!draft && <ErrorNotice error={error} />}
      {!config && loading ? (
        <Loading label={t('正在读取供应商配置…', 'Loading provider configuration…')} />
      ) : null}
      <div className="provider-grid">
        {config?.providers.map((provider, index) => (
          <Card
            key={provider.id}
            className={`provider-card managed-provider ${provider.enabled ? '' : 'provider-disabled'}`}
            aria-label={t(`${provider.id} 供应商`, `${provider.id} provider`)}
          >
            <Card.Header>
              <div className={`provider-logo provider-${index % 3}`}>
                <Globe2 size={25} />
              </div>
              <div className="provider-card-name">
                <Card.Title>{providerName(provider.id)}</Card.Title>
                <Card.Description>{provider.id}</Card.Description>
              </div>
              <Switch
                size="sm"
                className="provider-enable"
                aria-label={t(`启用 ${providerName(provider.id)}`, `Enable ${providerName(provider.id)}`)}
                isSelected={provider.enabled}
                isDisabled={busy}
                onChange={(enabled) => toggle(provider, enabled)}
              >
                <Switch.Content>
                  <Switch.Control>
                    <Switch.Thumb />
                  </Switch.Control>
                </Switch.Content>
              </Switch>
            </Card.Header>
            <Card.Content>
              <div className="provider-card-state">
                <Chip
                  size="sm"
                  variant="soft"
                  className={provider.enabled ? 'provider-enabled-chip' : 'neutral-chip'}
                >
                  {provider.enabled ? t('已启用', 'Enabled') : t('未启用', 'Disabled')}
                </Chip>
                <span>
                  {savingId === provider.id ? (
                    <>
                      <RefreshCw className="spin" size={12} />
                      {t('正在保存…', 'Saving…')}
                    </>
                  ) : (
                    t(`选择顺序 #${index + 1}`, `Selection order #${index + 1}`)
                  )}
                </span>
              </div>
              <div className="provider-capabilities">
                {[
                  [t('创建邮箱', 'Create mailboxes'), true],
                  [t('接收邮件', 'Receive mail'), provider.capabilities.receive],
                  [t('发送邮件', 'Send mail'), provider.capabilities.send],
                  [t('附件', 'Attachments'), provider.capabilities.attachments],
                ].map(([label, supported]) => (
                  <span key={String(label)} className={supported ? '' : 'unsupported'}>
                    {supported ? <Check size={14} /> : <span className="dash">—</span>}
                    {label}
                  </span>
                ))}
              </div>
              <div className="provider-limit">
                <Clock3 size={15} />
                <span>{t('最长本地有效期', 'Maximum local lifetime')}</span>
                <strong>{formatDuration(provider.max_ttl_seconds)}</strong>
              </div>
            </Card.Content>
            <Card.Footer>
              <span>
                <Layers size={13} />
                {t('固定绑定供应商', 'Permanent provider binding')}
              </span>
              <Button
                size="sm"
                variant="tertiary"
                isDisabled={busy}
                aria-label={t(`编辑 ${providerName(provider.id)}`, `Edit ${providerName(provider.id)}`)}
                onPress={() => openEditor(provider, index)}
              >
                <Pencil size={14} />
                {t('编辑', 'Edit')}
              </Button>
            </Card.Footer>
          </Card>
        ))}
      </div>
      {config && !config.providers.length && (
        <Empty
          title={t('暂无已支持的供应商', 'No supported providers')}
          description={t(
            '当前版本未提供可配置的邮箱供应商。',
            'This version has no configurable mailbox providers.',
          )}
        />
      )}
      <p className="page-note">
        {t(
          '开关会立即保存至 config.yaml 并在线生效。停用后，该供应商已有邮箱的同步将在当前任务结束后暂停。邮箱始终绑定创建时的供应商；本地有效期不保证上游持续可用。',
          'Toggles save to config.yaml and take effect online. Disabling a provider pauses sync for its existing mailboxes after the current task finishes. Each mailbox stays with its original provider; its local lifetime does not guarantee upstream availability.',
        )}
      </p>
      {draft && editing && (
        <Modal.Backdrop
          className="provider-edit-backdrop"
          isOpen
          isDismissable={!busy}
          isKeyboardDismissDisabled={busy}
          onOpenChange={(open) => {
            if (!open) closeEditor();
          }}
        >
          <Modal.Container size="lg" scroll="inside" className="provider-edit-container">
            <Modal.Dialog className="provider-edit-dialog">
              {!busy && (
                <Modal.CloseTrigger aria-label={t('关闭供应商配置', 'Close provider configuration')} />
              )}
              <Modal.Header>
                <div className="dialog-icon">
                  <Globe2 size={24} />
                </div>
                <Modal.Heading>
                  {t(`编辑 ${providerName(draft.id)}`, `Edit ${providerName(draft.id)}`)}
                </Modal.Heading>
                <p>
                  {draft.id} ·{' '}
                  {t(
                    '保存后在线应用，Worker 在当前任务结束后采用',
                    'Applies online after saving; the Worker adopts it after its current task',
                  )}
                </p>
              </Modal.Header>
              <Modal.Body>
                <form id="provider-settings" onSubmit={save} className="config-form">
                  <ErrorNotice error={error} />
                  <fieldset disabled={busy} className="config-fields">
                    <div className="config-grid">
                      <ConfigField
                        label={t('网站地址', 'Website URL')}
                        type="url"
                        value={draft.index_url}
                        onChange={(value) => edit({ index_url: value })}
                        hint={t('供应商主页，仅用于说明。', 'The provider homepage, for reference only.')}
                      />
                      <ConfigField
                        label={t('API 地址', 'API URL')}
                        type="url"
                        required
                        value={draft.base_url}
                        onChange={(value) => edit({ base_url: value })}
                      />
                      <ConfigField
                        label={t('请求超时（秒）', 'Request timeout (seconds)')}
                        type="number"
                        min="0.001"
                        step="any"
                        required
                        value={draft.timeout_seconds}
                        onChange={(value) => edit({ timeout_seconds: value })}
                      />
                      <ConfigField
                        label={t('浏览器模拟', 'Browser impersonation')}
                        required
                        value={draft.impersonate}
                        onChange={(value) => edit({ impersonate: value })}
                        placeholder="chrome110"
                      />
                      <ConfigField
                        label={t('最长本地有效期（秒）', 'Maximum local lifetime (seconds)')}
                        type="number"
                        min="60"
                        max="31536000"
                        step="1"
                        required
                        value={draft.max_ttl_seconds}
                        onChange={(value) => edit({ max_ttl_seconds: value })}
                        hint={t(
                          '60 秒至 365 天。本地上限不保证上游可用时长。',
                          '60 seconds to 365 days. The local limit does not guarantee upstream availability.',
                        )}
                      />
                      <Select
                        className="config-priority"
                        value={draft.priority}
                        onChange={(value) => edit({ priority: String(value) })}
                        isDisabled={busy}
                        fullWidth
                      >
                        <Label>{t('选择优先级', 'Selection priority')}</Label>
                        <Select.Trigger>
                          <Select.Value />
                          <Select.Indicator />
                        </Select.Trigger>
                        <Select.Popover>
                          <ListBox>
                            {config!.providers.map((item, index) => (
                              <ListBox.Item
                                id={String(index)}
                                key={item.id}
                                textValue={t(`第 ${index + 1} 位`, `Position ${index + 1}`)}
                              >
                                {t(`第 ${index + 1} 位`, `Position ${index + 1}`)}
                                <ListBox.ItemIndicator />
                              </ListBox.Item>
                            ))}
                          </ListBox>
                        </Select.Popover>
                        <p className="field-hint">
                          {t(
                            '自动选择优先匹配靠前的已启用供应商。',
                            'Automatic selection prefers matching enabled providers earlier in the list.',
                          )}
                        </p>
                      </Select>
                    </div>
                    <div className="config-proxy">
                      <ConfigField
                        label={t('替换代理地址', 'Replace proxy URL')}
                        type="password"
                        autoComplete="new-password"
                        disabled={draft.clearProxy}
                        value={draft.proxy}
                        onChange={(value) => edit({ proxy: value })}
                        placeholder={t('留空保留原配置', 'Leave blank to keep the current configuration')}
                        hint={
                          editing.proxy_configured
                            ? t(
                                '代理已配置，现有地址不会显示。填写新地址可替换。',
                                'A proxy is configured. Its current URL is hidden; enter a new URL to replace it.',
                              )
                            : t(
                                '未配置代理。可填写包含认证信息的代理地址。',
                                'No proxy is configured. You can enter a proxy URL that includes credentials.',
                              )
                        }
                      />
                      {editing.proxy_configured && (
                        <label className="config-toggle">
                          <input
                            type="checkbox"
                            checked={draft.clearProxy}
                            onChange={(event) => edit({ clearProxy: event.target.checked, proxy: '' })}
                          />
                          <span>{t('移除已配置代理', 'Remove the configured proxy')}</span>
                        </label>
                      )}
                    </div>
                  </fieldset>
                </form>
              </Modal.Body>
              <Modal.Footer>
                <Button variant="tertiary" isDisabled={busy} onPress={closeEditor}>
                  {t('取消', 'Cancel')}
                </Button>
                <Button
                  form="provider-settings"
                  type="submit"
                  className="primary-action"
                  isDisabled={busy || !dirty}
                >
                  {savingId ? <RefreshCw size={16} className="spin" /> : <Save size={16} />}
                  {savingId ? t('正在保存…', 'Saving…') : t('保存供应商', 'Save provider')}
                </Button>
              </Modal.Footer>
            </Modal.Dialog>
          </Modal.Container>
        </Modal.Backdrop>
      )}
    </div>
  );
}
