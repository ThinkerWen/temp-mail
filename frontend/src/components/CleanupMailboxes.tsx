import { Button, Modal } from '@heroui/react';
import { Info, RefreshCw, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { useI18n } from '../preferences';
import type { CleanupPreview } from '../types';
import { ErrorNotice, Loading } from './Common';

export default function CleanupMailboxes({
  token,
  onCleared,
  onUnauthorized,
}: {
  token: string;
  onCleared: (count: number) => void;
  onUnauthorized: (code: string) => void;
}) {
  const t = useI18n();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [phase, setPhase] = useState<'idle' | 'loading' | 'clearing'>('idle');
  const [error, setError] = useState<unknown>(null);
  const [changed, setChanged] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const busy = phase !== 'idle';

  useEffect(() => {
    return () => controller.current?.abort();
  }, [token]);

  function report(cause: unknown) {
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized(cause.code);
    else setError(cause);
  }

  async function loadPreview() {
    if (controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setPhase('loading');
    setError(null);
    setPreview(null);
    try {
      const value = await api.cleanupPreview(token, request.signal);
      if (!request.signal.aborted) setPreview(value);
    } catch (cause) {
      if (!request.signal.aborted) report(cause);
    } finally {
      if (controller.current === request) controller.current = null;
      if (!request.signal.aborted) setPhase('idle');
    }
  }

  async function clear() {
    if (controller.current || !preview || preview.count === 0) return;
    const request = new AbortController();
    controller.current = request;
    setPhase('clearing');
    setError(null);
    try {
      const result = await api.cleanupMailboxes(
        token,
        { cutoff: preview.cutoff, expected_count: preview.count, revision: preview.revision },
        request.signal,
      );
      if (!request.signal.aborted) {
        setOpen(false);
        onCleared(result.deleted_count);
      }
    } catch (cause) {
      if (request.signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 409 && cause.code === 'CLEANUP_CHANGED') {
        setChanged(true);
        setPreview(null);
        setPhase('loading');
        try {
          const value = await api.cleanupPreview(token, request.signal);
          if (!request.signal.aborted) setPreview(value);
        } catch (refreshError) {
          if (!request.signal.aborted) report(refreshError);
        }
      } else report(cause);
    } finally {
      if (controller.current === request) controller.current = null;
      if (!request.signal.aborted) setPhase('idle');
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="secondary"
        isDisabled={busy}
        onPress={() => {
          if (controller.current) return;
          setChanged(false);
          setOpen(true);
          void loadPreview();
        }}
      >
        <Trash2 size={14} />
        {t('清除失效邮箱', 'Clear expired mailboxes')}
      </Button>
      <Modal.Backdrop
        isOpen={open}
        isDismissable={!busy}
        isKeyboardDismissDisabled={busy}
        onOpenChange={(value) => {
          if (!controller.current) setOpen(value);
        }}
      >
        <Modal.Container size="sm">
          <Modal.Dialog className="create-dialog">
            {!busy && <Modal.CloseTrigger aria-label={t('关闭清除窗口', 'Close cleanup dialog')} />}
            <Modal.Header>
              <div className="dialog-icon">
                <Trash2 size={24} />
              </div>
              <Modal.Heading>{t('清除失效邮箱', 'Clear expired mailboxes')}</Modal.Heading>
              <p>
                {t(
                  '统计全部失效邮箱，不受当前搜索和分页影响。',
                  'Includes all expired mailboxes, regardless of the current search or page.',
                )}
              </p>
            </Modal.Header>
            <Modal.Body>
              <div className="create-form">
                {changed && (
                  <div className="notice" role="status">
                    <Info size={17} />
                    <span>
                      {t(
                        '待清除的邮箱已变化。请核对最新统计后再次确认。',
                        'The eligible mailboxes changed. Review the latest count and confirm again.',
                      )}
                    </span>
                  </div>
                )}
                {phase === 'loading' ? (
                  <Loading label={t('正在统计失效邮箱…', 'Counting expired mailboxes…')} />
                ) : preview ? (
                  <p role="status">
                    {preview.count > 0
                      ? t(
                          `将清除 ${preview.count} 个失效邮箱。`,
                          `${preview.count} expired mailboxes will be cleared.`,
                        )
                      : t('当前没有可清除的失效邮箱。', 'There are no expired mailboxes to clear.')}
                  </p>
                ) : null}
                <p className="field-hint">
                  {t(
                    '这会永久清除本地失效邮箱及其邮件缓存，操作记录会保留。',
                    'This permanently removes local expired mailboxes and their cached messages. Operation history is retained.',
                  )}
                </p>
                <ErrorNotice
                  error={error}
                  retry={busy ? undefined : () => void (preview ? clear() : loadPreview())}
                />
              </div>
            </Modal.Body>
            <Modal.Footer>
              <Button
                variant="tertiary"
                isDisabled={busy}
                onPress={() => {
                  if (!controller.current) setOpen(false);
                }}
              >
                {t('取消', 'Cancel')}
              </Button>
              <Button
                className="primary-action"
                isDisabled={busy || !preview || preview.count === 0}
                onPress={() => void clear()}
              >
                {phase === 'clearing' ? <RefreshCw size={16} className="spin" /> : <Trash2 size={16} />}
                {phase === 'clearing'
                  ? t('正在清除…', 'Clearing…')
                  : preview
                    ? t(`确认清除 ${preview.count} 个邮箱`, `Clear ${preview.count} mailboxes`)
                    : t('确认清除', 'Confirm cleanup')}
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </>
  );
}
