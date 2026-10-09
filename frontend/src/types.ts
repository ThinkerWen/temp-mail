export interface Capabilities {
  receive: boolean;
  send: boolean;
  delete: boolean;
  attachments: boolean;
  webhook: boolean;
  custom_local_part: boolean;
  max_ttl_seconds: number;
  destructive_receive?: boolean;
}

export interface Provider {
  id: string;
  capabilities: Capabilities;
}
export interface Mailbox {
  id: string;
  email: string;
  provider_id: string;
  capabilities: Capabilities;
  status: 'active' | 'expired' | 'deleted';
  created_at: string;
  expires_at: string;
  last_synced_at: string | null;
  last_sync_error_code: string | null;
}
export interface MessageSummary {
  id: string;
  mailbox_id: string;
  sender: string;
  recipients: string[];
  subject: string;
  received_at: string;
}
export interface Message extends MessageSummary {
  text: string;
}
export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
}
export interface MessagePage extends Page<MessageSummary> {
  last_synced_at: string | null;
}
export interface CreatePayload {
  provider: string;
  ttl_seconds: number;
  required_capabilities: ['receive'];
}
export interface Draft {
  key: string;
  payload: CreatePayload;
}
export type OperationStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'unknown';
export interface Operation {
  id: string;
  kind: 'create' | 'send' | 'delete';
  status: OperationStatus;
  provider_id: string;
  mailbox_id: string | null;
  result: { mailbox_id?: string; email?: string } | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
}
