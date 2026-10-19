import type { ChannelEvent, ChannelStatus, Filter } from './types';

const MAX_BACKOFF_MS = 15_000;
const BASE_BACKOFF_MS = 500;

type MessageListener = (event: ChannelEvent) => void;
type StatusListener = (status: ChannelStatus) => void;

interface Channel {
  path: string;
  filter: Filter | null;
  ws: WebSocket | null;
  status: ChannelStatus;
  messageListeners: Set<MessageListener>;
  statusListeners: Set<StatusListener>;
  retries: number;
  closed: boolean;
  reconnectTimer: number | null;
  lastMessageAt: number | null;
  messageCount: number;
}

const channels = new Map<string, Channel>();
type SnapshotListener = () => void;
const snapshotListeners = new Set<SnapshotListener>();
function notifySnapshotListeners(): void {
  for (const l of snapshotListeners) l();
}

function channelKey(path: string, filter: Filter | null): string {
  return `${path}::${filter ? JSON.stringify(filter) : ''}`;
}

function wsUrl(path: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}${path}`;
}

function setStatus(channel: Channel, next: ChannelStatus): void {
  if (channel.status === next) return;
  channel.status = next;
  for (const listener of channel.statusListeners) listener(next);
  notifySnapshotListeners();
}

function connect(channel: Channel): void {
  if (channel.closed) return;
  setStatus(channel, channel.retries === 0 ? 'connecting' : 'reconnecting');

  const ws = new WebSocket(wsUrl(channel.path));
  channel.ws = ws;

  ws.addEventListener('open', () => {
    channel.retries = 0;
    setStatus(channel, 'connected');
    ws.send(JSON.stringify(channel.filter ?? {}));
  });

  ws.addEventListener('message', (e) => {
    let parsed: ChannelEvent | null = null;
    try {
      const raw: unknown = JSON.parse(e.data as string);
      if (raw !== null && typeof raw === 'object') parsed = raw as ChannelEvent;
    } catch {
      return;
    }
    if (parsed === null) return;
    // Heartbeats from the daemon (ws.py ``_ws_channel``) keep the connection
    // alive every ~20s with ``{"type": "ping"}``. They carry no payload and
    // must not reach component handlers, otherwise they render as empty
    // ghost rows in the feeds (all fields absent → fallback key ``ws::``).
    if ((parsed as { type?: unknown }).type === 'ping') return;
    channel.lastMessageAt = Date.now();
    channel.messageCount += 1;
    for (const listener of channel.messageListeners) listener(parsed);
  });

  ws.addEventListener('close', (e) => {
    channel.ws = null;
    if (channel.closed) {
      setStatus(channel, 'disconnected');
      return;
    }
    // 1008 = policy violation (auth failed); don't retry — the 401 handler will redirect.
    if (e.code === 1008) {
      setStatus(channel, 'disconnected');
      return;
    }
    scheduleReconnect(channel);
  });

  ws.addEventListener('error', () => {
    // error events always precede a close; handle there.
  });
}

function scheduleReconnect(channel: Channel): void {
  channel.retries += 1;
  const exp = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** (channel.retries - 1));
  const jittered = exp * (0.7 + Math.random() * 0.6);
  setStatus(channel, 'reconnecting');
  channel.reconnectTimer = window.setTimeout(() => {
    channel.reconnectTimer = null;
    connect(channel);
  }, jittered);
}

export interface Subscription {
  unsubscribe: () => void;
  getStatus: () => ChannelStatus;
  subscribeStatus: (listener: StatusListener) => () => void;
}

export function subscribe(
  path: string,
  filter: Filter | null,
  onMessage: MessageListener,
): Subscription {
  const key = channelKey(path, filter);
  let channel = channels.get(key);
  if (!channel) {
    channel = {
      path,
      filter,
      ws: null,
      status: 'connecting',
      messageListeners: new Set(),
      statusListeners: new Set(),
      retries: 0,
      closed: false,
      reconnectTimer: null,
      lastMessageAt: null,
      messageCount: 0,
    };
    channels.set(key, channel);
    notifySnapshotListeners();
    connect(channel);
  }
  channel.messageListeners.add(onMessage);

  return {
    unsubscribe: () => {
      if (!channel) return;
      channel.messageListeners.delete(onMessage);
      if (channel.messageListeners.size === 0) {
        channel.closed = true;
        if (channel.reconnectTimer !== null) {
          window.clearTimeout(channel.reconnectTimer);
          channel.reconnectTimer = null;
        }
        channel.ws?.close();
        channels.delete(key);
        notifySnapshotListeners();
      }
    },
    getStatus: () => channel.status,
    subscribeStatus: (listener) => {
      channel.statusListeners.add(listener);
      return () => channel.statusListeners.delete(listener);
    },
  };
}

export interface ChannelSnapshot {
  path: string;
  filterKey: string;
  status: ChannelStatus;
  retries: number;
  lastMessageAt: number | null;
  messageCount: number;
}

/** Point-in-time view of every active channel. Not reactive by itself —
 * pair with ``subscribeChannelSnapshots`` if you need updates. */
export function listChannels(): ChannelSnapshot[] {
  const out: ChannelSnapshot[] = [];
  for (const c of channels.values()) {
    out.push({
      path: c.path,
      filterKey: c.filter ? JSON.stringify(c.filter) : '',
      status: c.status,
      retries: c.retries,
      lastMessageAt: c.lastMessageAt,
      messageCount: c.messageCount,
    });
  }
  return out;
}

/** Fires on channel add/remove and on any status transition. Message ticks
 * are NOT notified — polling at interaction time is cheaper than thrashing
 * every component each time a WS event lands. */
export function subscribeChannelSnapshots(listener: SnapshotListener): () => void {
  snapshotListeners.add(listener);
  return () => {
    snapshotListeners.delete(listener);
  };
}
