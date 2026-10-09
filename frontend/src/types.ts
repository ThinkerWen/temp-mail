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
export interface ProviderConfig {
  id: string;
  enabled: boolean;
  index_url: string;
  base_url: string;
  timeout_seconds: number;
  impersonate: string;
  proxy_configured: boolean;
  max_ttl_seconds: number;
  capabilities: Capabilities;
}
export interface Configuration {
  revision: string;
  restart_required: boolean;
  restart_required_fields: string[];
  app: { db_path: string; api_token_configured: boolean; encryption_key_configured: boolean };
  worker: {
    sync_interval_seconds: number;
    operation_timeout_seconds: number;
    poll_seconds: number;
    create_concurrency: number;
    receive_concurrency: number;
  };
  providers: ProviderConfig[];
}
export interface ConfigurationPatch {
  revision: string;
  app?: { db_path?: string; api_token?: string };
  worker?: Partial<Configuration['worker']>;
  providers?: (Omit<ProviderConfig, 'proxy_configured' | 'capabilities'> & { proxy?: string | null })[];
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
  total?: number;
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
export interface OperationPage extends Page<Operation> {
  total: number;
}
export interface DashboardData {
  generated_at: string;
  mailboxes: { total: number; active: number; expired: number; deleted: number; sync_errors: number };
  messages: { total: number; received_24h: number };
  operations: {
    total: number;
    pending: number;
    running: number;
    succeeded: number;
    failed: number;
    unknown: number;
  };
  last_synced_at: string | null;
  activity: { date: string; mailboxes: number; messages: number }[];
  recent_operations: Operation[];
  provider_stats: { id: string; mailboxes: number; active: number }[];
}
export interface CleanupPreview {
  count: number;
  cutoff: string;
  revision: string;
}
export interface CleanupRequest {
  cutoff: string;
  expected_count: number;
  revision: string;
}
