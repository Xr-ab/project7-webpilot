/**
 * 语音会话的 WebSocket 客户端封装（P6b-B5）。
 *
 * 它在项目里的角色：把"连上 / 重连 / 收发 JSON / 断开"这四件事从 UI 里拆出来。
 * VoiceSession.tsx 只关心"收到什么事件、该怎么显示"，
 * 而"断了要不要重连、重连几次、什么时候放弃"是这个文件的事。
 *
 * 三条设计纪律（都来自 P2 踩过的坑）：
 *
 * 1. **token 走首帧，不走 URL**。`ws://host/ws?token=xxx` 会把凭证写进
 *    uvicorn 访问日志、浏览器历史、代理日志——和 P5b 把媒体地址改成签名 URL
 *    是同一个思路：身份凭证不该出现在 URL 里。
 *
 * 2. **重连 3 次就停**，不无限重试。语音永远是增强项：连不上时用户还能用
 *    文本表单，而无限重连会让界面一直显示"连接中"，比直接说"语音不可用"更糟。
 *    退避 1s / 3s / 9s：本地服务重启通常几秒内就绪，等太久没意义。
 *
 * 3. **不缓存消息**。断线期间发生的事（任务进度）不补发——
 *    推送是"实时视图"，不是"可靠消息队列"；补发会造出重复的 task.final，
 *    让播放队列冒出第二段播报。要查历史就去 REST 查 trace。
 */

export type VoiceEvent = {
  type: string;
  [key: string]: unknown;
};

export type SessionStatus = "connecting" | "ready" | "reconnecting" | "closed" | "failed";

export interface VoiceSocketOptions {
  /** 收到一条服务端事件 */
  onEvent: (event: VoiceEvent) => void;
  /** 连接状态变化（UI 用来显示"连接中/已连接/不可用"） */
  onStatus: (status: SessionStatus, detail?: string) => void;
}

/** 重连退避（毫秒）。长度就是"最多重连几次"。 */
const RECONNECT_DELAYS = [1000, 3000, 9000];

export class VoiceSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUser = false;
  private token: string;
  private options: VoiceSocketOptions;

  constructor(token: string, options: VoiceSocketOptions) {
    this.token = token;
    this.options = options;
  }

  /** 建立连接（并完成首帧认证）。 */
  connect(): void {
    this.closedByUser = false;
    this.options.onStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    this.open();
  }

  private open(): void {
    // 同源推导地址：前端 dev server 在 5173，后端在 8000，
    // 所以不能直接用 location.host。开发环境写死 8000，生产同源时可用 location.host。
    // 用 wss 的判断跟页面协议走：https 页面连 ws:// 会被浏览器直接拦掉。
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const host = import.meta.env.DEV ? "127.0.0.1:8000" : window.location.host;
    const ws = new WebSocket(`${scheme}://${host}/api/v1/voice/ws`);
    this.ws = ws;

    ws.onopen = () => {
      // 首帧必须是 session.start——服务端 5 秒收不到就关连接
      ws.send(JSON.stringify({ type: "session.start", token: this.token }));
    };

    ws.onmessage = (raw) => {
      let event: VoiceEvent;
      try {
        event = JSON.parse(raw.data as string) as VoiceEvent;
      } catch {
        return; // 脏消息丢掉即可，不该让整个会话崩掉
      }
      if (event.type === "session.ready") {
        this.attempt = 0; // 连上了就重置退避计数
        this.options.onStatus("ready");
      }
      if (event.type === "pong") return;
      this.options.onEvent(event);
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (this.closedByUser) {
        this.options.onStatus("closed");
        return;
      }
      // 认证失败/被服务端拒绝（4001/4003/4004）不重连：重连也不会变好，
      // 而且会让用户对着一个"永远连不上"的界面反复看到"重连中"
      if (ev.code === 4001 || ev.code === 4003 || ev.code === 4004) {
        this.options.onStatus("failed", "语音会话被服务端拒绝（登录状态或功能开关）");
        return;
      }
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // onerror 之后一定会 onclose，重连逻辑只写在 onclose 里，避免两处各连一次
    };
  }

  private scheduleReconnect(): void {
    if (this.attempt >= RECONNECT_DELAYS.length) {
      this.options.onStatus("failed", "语音服务连不上，请改用文本提交任务");
      return;
    }
    const delay = RECONNECT_DELAYS[this.attempt];
    this.attempt += 1;
    this.options.onStatus("reconnecting", `${Math.round(delay / 1000)} 秒后重连`);
    window.setTimeout(() => {
      if (!this.closedByUser) this.open();
    }, delay);
  }

  /** 发一条消息。没连上时静默丢弃（调用方不需要为"断线了"写分支）。 */
  send(payload: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(payload));
    return true;
  }

  get isReady(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  close(): void {
    this.closedByUser = true;
    this.send({ type: "session.end" });
    this.ws?.close();
    this.ws = null;
  }
}
