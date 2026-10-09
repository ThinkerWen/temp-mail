import { Button, Input, Label, TextField } from '@heroui/react';
import { ArrowRight, Check, KeyRound, Layers, LockKeyhole, Mail, RefreshCw, ShieldCheck } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { useI18n } from '../preferences';
import { Brand, ErrorNotice } from './Common';

export default function Login({ onConnect, reason }: { onConnect: (token: string) => void; reason: string }) {
  const t = useI18n();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(reason || null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.providers(token.trim());
      onConnect(token.trim());
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <section className="login-content">
        <Brand />
        <div className="login-form-wrap">
          <span className="eyebrow">
            <span /> {t('一个入口，多个邮箱', 'One workspace, many mailboxes')}
          </span>
          <h1>
            {t('给收件箱，', 'Give your inbox')}
            <br />
            {t('多一份', 'some ')}
            <span>{t('隐私。', 'privacy.')}</span>
          </h1>
          <p className="login-description">
            {t('创建临时地址，接收重要消息。', 'Create temporary addresses. Receive what matters.')}
            <br />
            {t('把你的邮箱，井井有条地放在一起。', 'Keep all your mailboxes neatly in one place.')}
          </p>
          <form onSubmit={(event) => void submit(event)} className="login-form">
            <TextField
              type="password"
              value={token}
              onChange={setToken}
              isRequired
              isDisabled={busy}
              autoComplete="off"
            >
              <Label>{t('访问令牌', 'Access token')}</Label>
              <div className="token-input">
                <KeyRound size={17} />
                <Input
                  placeholder={t('输入你的 API 访问令牌', 'Enter your API access token')}
                  aria-describedby="token-help"
                />
              </div>
            </TextField>
            <p id="token-help" className="field-hint">
              {t(
                '使用服务配置中的 app.api_token 连接工作台。',
                'Connect using app.api_token from your service configuration.',
              )}
            </p>
            <ErrorNotice error={error} />
            <Button type="submit" className="primary-action login-submit" isDisabled={!token.trim() || busy}>
              {busy ? <RefreshCw size={17} className="spin" /> : <ArrowRight size={17} />}
              {busy ? t('正在连接…', 'Connecting…') : t('连接工作台', 'Connect to workspace')}
            </Button>
          </form>
          <p className="login-private">
            <LockKeyhole size={13} />
            {t(
              '令牌仅保存在当前标签页，退出后清除。',
              'Your token stays in this tab and is cleared when you disconnect.',
            )}
          </p>
        </div>
        <footer>
          {t('你的临时邮箱，始终有序。', 'Temporary mail, always organized.')}
          <span>Temp Mail Workspace</span>
        </footer>
      </section>
      <section
        className="login-visual"
        aria-label={t('统一邮箱管理介绍', 'About your unified mail workspace')}
      >
        <div className="visual-label">
          <span className="tiny-dot" />{' '}
          {t('为短暂的消息，留一个专属位置', 'A place for messages that pass through')}
        </div>
        <div className="mail-illustration" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="floating-note note-top">
            <ShieldCheck size={18} />
            <div>
              <strong>{t('独立的临时地址', 'Your own temporary address')}</strong>
              <small>{t('让日常收件更轻松', 'Make everyday mail easier')}</small>
            </div>
          </div>
          <div className="letter-back" />
          <div className="letter">
            <div className="letter-icon">
              <Mail size={25} />
            </div>
            <span className="letter-line long" />
            <span className="letter-line" />
            <span className="letter-line medium" />
            <span className="letter-stamp">
              <Check size={18} />
            </span>
          </div>
          <div className="envelope">
            <div className="envelope-flap" />
            <Mail size={32} strokeWidth={1.2} />
          </div>
          <div className="floating-note note-bottom">
            <Layers size={19} />
            <div>
              <strong>{t('多个供应商，一个工作台', 'Many providers, one workspace')}</strong>
              <small>{t('创建、收件、阅读', 'Create, receive, read')}</small>
            </div>
          </div>
          <span className="spark spark-one">✦</span>
          <span className="spark spark-two">✦</span>
        </div>
        <div className="visual-copy">
          <h2>
            {t('临时邮箱。', 'Temporary mail.')}
            <br />
            {t('完整的收件体验。', 'A complete inbox experience.')}
          </h2>
          <p>{t('地址会到期，有序的工作方式不会。', 'Addresses expire. Good organization stays.')}</p>
        </div>
        <div className="visual-footer">
          <span>{t('轻量', 'Lightweight')}</span>
          <i />
          <span>{t('统一管理', 'All in one place')}</span>
          <i />
          <span>{t('自动收件', 'Automatic sync')}</span>
        </div>
      </section>
    </div>
  );
}
