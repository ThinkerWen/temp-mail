import { Button, Input, Label, TextField } from '@heroui/react';
import { Check, HardDrive, Info, KeyRound, RefreshCw, Save, Settings2 } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent, type InputHTMLAttributes } from 'react';
import { api, ApiError, restartRequiredMessage } from '../api';
import { useI18n } from '../preferences';
import type { Configuration, ConfigurationPatch } from '../types';
import { ErrorNotice, Loading } from './Common';

type ConfigDraft = {
  db_path: string;
  api_token: string;
  sync_interval_seconds: string;
  operation_timeout_seconds: string;
  poll_seconds: string;
  create_concurrency: string;
  receive_concurrency: string;
};

function draftFrom(config: Configuration): ConfigDraft {
  return {
    db_path: config.app.db_path,
    api_token: '',
    sync_interval_seconds: String(config.worker.sync_interval_seconds),
    operation_timeout_seconds: String(config.worker.operation_timeout_seconds),
    poll_seconds: String(config.worker.poll_seconds),
    create_concurrency: String(config.worker.create_concurrency),
    receive_concurrency: String(config.worker.receive_concurrency),
  };
}

export function ConfigField({
  label,
  hint,
  value,
  onChange,
  ...props
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'size'>) {
  return (
    <TextField className="config-field" value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input {...props} />
      {hint && <p className="field-hint">{hint}</p>}
    </TextField>
  );
}

export default function ConfigEditor({
  token,
  disconnect,
  onSaved,
  onTokenChangePending,
}: {
  token: string;
  disconnect: (message?: string) => void;
  onSaved: (config: Configuration, options: { tokenChanged: boolean }) => void;
  onTokenChangePending: (pending: boolean) => void;
}) {
  const t = useI18n();
  const [config, setConfig] = useState<Configuration | null>(null);
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
  const [baseline, setBaseline] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const changingToken = useRef(false);
  const dirty = !!draft && JSON.stringify(draft) !== baseline;
  const busy = loading || saving;

  function accept(value: Configuration) {
    const next = draftFrom(value);
    setConfig(value);
    setDraft(next);
    setBaseline(JSON.stringify(next));
  }
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
        if (!request.signal.aborted) accept(value);
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
    return () => {
      controller.current?.abort();
      if (changingToken.current) {
        changingToken.current = false;
        onTokenChangePending(false);
      }
    };
  }, [token, disconnect, onTokenChangePending]);

  useEffect(() => {
    if (!dirty && !saving) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    const beforeNavigate = (event: Event) => {
      if (
        saving ||
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
  }, [dirty, saving, t]);

  async function reload() {
    if (inFlight.current || busy) return;
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
    setLoading(true);
    setError(null);
    setSaved(false);
    try {
      const value = await api.configuration(token, request.signal);
      if (!request.signal.aborted) accept(value);
    } catch (cause) {
      if (!request.signal.aborted) report(cause);
    } finally {
      inFlight.current = false;
      if (!request.signal.aborted) setLoading(false);
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft || !config || inFlight.current || busy || !dirty) return;
    inFlight.current = true;
    const request = new AbortController();
    controller.current = request;
    const payload: ConfigurationPatch = { revision: config.revision };
    payload.app = { db_path: draft.db_path };
    if (draft.api_token.trim()) payload.app.api_token = draft.api_token;
    const tokenChanged = !!payload.app.api_token && payload.app.api_token !== token;
    payload.worker = {
      sync_interval_seconds: Number(draft.sync_interval_seconds),
      operation_timeout_seconds: Number(draft.operation_timeout_seconds),
      poll_seconds: Number(draft.poll_seconds),
      create_concurrency: Number(draft.create_concurrency),
      receive_concurrency: Number(draft.receive_concurrency),
    };
    setSaving(true);
    setError(null);
    setSaved(false);
    if (tokenChanged) {
      changingToken.current = true;
      onTokenChangePending(true);
    }
    try {
      const value = await api.saveConfiguration(token, payload, request.signal);
      if (!request.signal.aborted) {
        accept(value);
        setSaved(true);
        onSaved(value, { tokenChanged });
      }
    } catch (cause) {
      if (!request.signal.aborted) report(cause);
    } finally {
      inFlight.current = false;
      if (changingToken.current) {
        changingToken.current = false;
        onTokenChangePending(false);
      }
      if (!request.signal.aborted) setSaving(false);
    }
  }

  function edit(key: keyof ConfigDraft, value: string) {
    setDraft((current) => current && { ...current, [key]: value });
    setSaved(false);
  }

  return (
    <section className="config-editor" aria-label={t('系统配置编辑器', 'System configuration editor')}>
      <div className="section-intro">
        <div>
          <h2>{t('系统配置', 'System settings')}</h2>
          <p>
            {t(
              '保存至 config.yaml 并在线应用。Worker 会自动采用新配置，正在执行的任务不受影响。',
              'Save to config.yaml and apply settings online. The Worker adopts changes without interrupting running tasks.',
            )}
          </p>
        </div>
        <Settings2 size={27} strokeWidth={1.3} />
      </div>
      {config?.restart_required && (
        <div className="notice config-restart" role="status">
          <Info size={18} />
          <span>{restartRequiredMessage(config)}</span>
        </div>
      )}
      <ErrorNotice error={error} />
      {loading && !draft ? <Loading label={t('正在读取配置…', 'Loading configuration…')} /> : null}
      {!draft && !loading && (
        <Button variant="secondary" onPress={() => void reload()}>
          {t('重新读取配置', 'Reload configuration')}
        </Button>
      )}
      {draft && config && (
        <form className="config-form" onSubmit={(event) => void save(event)}>
          <fieldset disabled={busy} className="config-fields">
            <section className="config-card">
              <div className="config-card-heading">
                <span className="config-icon">
                  <HardDrive size={21} />
                </span>
                <div>
                  <h3>{t('存储与访问', 'Storage and access')}</h3>
                  <p>{t('工作空间的基础配置', 'Core workspace configuration')}</p>
                </div>
              </div>
              <div className="config-grid">
                <ConfigField
                  label={t('数据库路径', 'Database path')}
                  value={draft.db_path}
                  onChange={(value) => edit('db_path', value)}
                  required
                  hint={t(
                    '重启后使用此路径；修改路径不会自动迁移已有邮箱和邮件数据。',
                    'This path takes effect after restarting. Changing it does not migrate existing mailboxes or messages.',
                  )}
                />
                <ConfigField
                  label={t('新的访问令牌', 'New access token')}
                  type="password"
                  autoComplete="new-password"
                  minLength={24}
                  value={draft.api_token}
                  onChange={(value) => edit('api_token', value)}
                  placeholder={t('留空保留原令牌', 'Leave blank to keep the current token')}
                  hint={t(
                    `${config.app.api_token_configured ? '当前已配置。' : '当前未配置。'}新令牌至少 24 位，保存后立即生效。当前会话将退出，请使用新令牌重新登录。`,
                    `${config.app.api_token_configured ? 'A token is configured.' : 'No token is configured.'} Use at least 24 characters. Saving activates the new token immediately and signs you out; sign in again with the new token.`,
                  )}
                />
              </div>
              <div className="config-protected">
                <KeyRound size={18} />
                <div>
                  <strong>
                    {t('加密密钥', 'Encryption key')}{' '}
                    <span>
                      {config.app.encryption_key_configured
                        ? t('已配置', 'Configured')
                        : t('未配置', 'Not configured')}
                    </span>
                  </strong>
                  <p>
                    {t(
                      '用于解密已保存的邮箱凭据。更换需离线迁移已有数据，运行期间不支持修改。',
                      'Decrypts stored mailbox credentials. Changing it requires offline data migration and is unavailable while the service is running.',
                    )}
                  </p>
                </div>
              </div>
            </section>
            <section className="config-card">
              <div className="config-card-heading">
                <span className="config-icon">
                  <RefreshCw size={21} />
                </span>
                <div>
                  <h3>{t('后台处理', 'Background processing')}</h3>
                  <p>
                    {t(
                      '创建与收件独立并行。调低并发不会中断正在执行的任务，待任务完成后按新上限调度。',
                      'Creation and receiving run independently. Lowering concurrency lets running tasks finish before scheduling within the new limits.',
                    )}
                  </p>
                </div>
              </div>
              <div className="config-grid">
                <ConfigField
                  label={t('创建邮箱并发', 'Mailbox creation concurrency')}
                  type="number"
                  min="1"
                  max="32"
                  step="1"
                  required
                  value={draft.create_concurrency}
                  onChange={(value) => edit('create_concurrency', value)}
                  hint={t(
                    '同时创建邮箱的任务数，1–32。',
                    'Simultaneous mailbox creation tasks, from 1 to 32.',
                  )}
                />
                <ConfigField
                  label={t('收取邮件并发', 'Mail receiving concurrency')}
                  type="number"
                  min="1"
                  max="32"
                  step="1"
                  required
                  value={draft.receive_concurrency}
                  onChange={(value) => edit('receive_concurrency', value)}
                  hint={t(
                    '同时收取邮件的邮箱数，1–32。',
                    'Mailboxes checked for mail simultaneously, from 1 to 32.',
                  )}
                />
              </div>
              <div className="config-grid config-grid-three">
                <ConfigField
                  label={t('邮件同步间隔（秒）', 'Mail sync interval (seconds)')}
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={draft.sync_interval_seconds}
                  onChange={(value) => edit('sync_interval_seconds', value)}
                  hint={t('后台定期检查邮箱的新邮件。', 'How often the worker checks for new messages.')}
                />
                <ConfigField
                  label={t('操作超时（秒）', 'Operation timeout (seconds)')}
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={draft.operation_timeout_seconds}
                  onChange={(value) => edit('operation_timeout_seconds', value)}
                  hint={t(
                    '用于识别长时间未完成的操作。',
                    'Identifies operations that have not completed in time.',
                  )}
                />
                <ConfigField
                  label={t('队列检查间隔（秒）', 'Queue polling interval (seconds)')}
                  type="number"
                  min="0.001"
                  step="any"
                  required
                  value={draft.poll_seconds}
                  onChange={(value) => edit('poll_seconds', value)}
                  hint={t('后台检查待处理任务的间隔。', 'How often the worker checks for pending tasks.')}
                />
              </div>
            </section>
          </fieldset>
          <div className="config-savebar">
            <div className={`config-save-status ${saved ? 'saved' : ''}`} role="status">
              {saved ? (
                <>
                  <Check size={17} />
                  <span>
                    {t(
                      '配置已保存到 config.yaml，在线设置已生效。',
                      'Configuration saved to config.yaml. Online settings are active.',
                    )}
                  </span>
                </>
              ) : (
                <>
                  <span className={`config-status-dot ${dirty ? 'dirty' : ''}`} />
                  <span>
                    {saving
                      ? t('正在写入配置文件…', 'Writing configuration…')
                      : dirty
                        ? t('有尚未保存的修改', 'You have unsaved changes')
                        : t('与配置文件一致', 'Matches the configuration file')}
                  </span>
                </>
              )}
            </div>
            <div className="config-save-actions">
              <Button type="button" variant="tertiary" isDisabled={busy} onPress={() => void reload()}>
                <RefreshCw size={16} className={loading ? 'spin' : ''} />
                {t('重新读取配置', 'Reload configuration')}
              </Button>
              <Button type="submit" className="primary-action" isDisabled={busy || !dirty}>
                {saving ? <RefreshCw size={16} className="spin" /> : <Save size={16} />}
                {saving ? t('正在保存…', 'Saving…') : t('保存配置', 'Save configuration')}
              </Button>
            </div>
          </div>
        </form>
      )}
    </section>
  );
}
