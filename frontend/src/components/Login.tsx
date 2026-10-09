import { Button, Input, Label, TextField } from '@heroui/react';
import { ArrowRight, Check, KeyRound, Layers, LockKeyhole, Mail, RefreshCw, ShieldCheck } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { Brand, ErrorNotice } from './Common';

export default function Login({ onConnect, reason }: { onConnect: (token: string) => void; reason: string }) {
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
            <span /> 一个入口，多个邮箱
          </span>
          <h1>
            给收件箱，
            <br />
            留一点<span>自由。</span>
          </h1>
          <p className="login-description">
            创建临时地址，接收重要消息。
            <br />
            把你的邮箱，井井有条地放在一起。
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
              <Label>访问令牌</Label>
              <div className="token-input">
                <KeyRound size={17} />
                <Input placeholder="输入你的 API 访问令牌" aria-describedby="token-help" />
              </div>
            </TextField>
            <p id="token-help" className="field-hint">
              使用服务配置中的 app.api_token 连接工作台。
            </p>
            <ErrorNotice error={error} />
            <Button type="submit" className="primary-action login-submit" isDisabled={!token.trim() || busy}>
              {busy ? <RefreshCw size={17} className="spin" /> : <ArrowRight size={17} />}
              {busy ? '正在连接…' : '连接工作台'}
            </Button>
          </form>
          <p className="login-private">
            <LockKeyhole size={13} />
            令牌仅保存在当前标签页，退出后清除。
          </p>
        </div>
        <footer>
          你的临时邮箱，始终有序。<span>Temp Mail Workspace</span>
        </footer>
      </section>
      <section className="login-visual" aria-label="统一邮箱管理介绍">
        <div className="visual-label">
          <span className="tiny-dot" /> 为短暂的消息，留一个专属位置
        </div>
        <div className="mail-illustration" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="floating-note note-top">
            <ShieldCheck size={18} />
            <div>
              <strong>独立的临时地址</strong>
              <small>让日常收件更轻松</small>
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
              <strong>多个供应商，一个工作台</strong>
              <small>创建、收件、阅读</small>
            </div>
          </div>
          <span className="spark spark-one">✦</span>
          <span className="spark spark-two">✦</span>
        </div>
        <div className="visual-copy">
          <h2>
            临时邮箱。
            <br />
            完整的收件体验。
          </h2>
          <p>地址会到期，有序的工作方式不会。</p>
        </div>
        <div className="visual-footer">
          <span>轻量</span>
          <i />
          <span>统一管理</span>
          <i />
          <span>自动收件</span>
        </div>
      </section>
    </div>
  );
}
