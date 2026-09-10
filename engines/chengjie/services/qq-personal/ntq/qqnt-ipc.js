/**
 * 边车 ⇄ QQ 主进程内 agent 的本机 IPC：JSON Lines over 命名管道（win32 \\.\pipe\…）/ Unix socket。
 *
 * 方向：边车开 server（本文件），agent 作为 client 连回来（agent 在 QQ 进程里，不监听端口）。
 * 鉴权：每次启动随机 32 字节 token 经 **环境变量** 传给 QQ 子进程（不落盘、不入日志），
 *       agent 首帧 {t:"hello", token} 不对即断开。管道只在本机可达，不经任何网络。
 * 帧：  {id, t:"call", m, p}  →  {id, t:"ret", ok, r|e}      边车调 agent
 *       {t:"ev", ev}                                            agent 推事件（登录态 / 消息 / 好友申请…）
 * 日志纪律：本层只记帧类型 / 方法名 / 字节数，**绝不记 p / r / ev 内容**（里面有消息正文与 uid）。
 */
import net from "node:net";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";

export function newPipePath(tag = "zhiliao-qq") {
  const rnd = crypto.randomBytes(6).toString("hex");
  if (process.platform === "win32") return `\\\\.\\pipe\\${tag}-${rnd}`;
  return path.join(os.tmpdir(), `${tag}-${rnd}.sock`);
}

export function newToken() { return crypto.randomBytes(32).toString("hex"); }

/** 边车侧：等 agent 连上并完成 hello 握手；之后 call()/onEvent()。单连接（QQ 只有一个主进程）。 */
export class AgentServer {
  constructor({ pipePath, token, logger, handshakeMs = 45000 }) {
    this.pipePath = pipePath; this.token = token; this.log = logger || console;
    this.handshakeMs = handshakeMs;
    this.sock = null; this.hello = null; this.evCb = null;
    this._pending = new Map(); this._seq = 0; this._server = null;
    this._closed = false;
  }

  listen() {
    return new Promise((resolve, reject) => {
      this._server = net.createServer((sock) => this._accept(sock));
      this._server.once("error", reject);
      this._server.listen(this.pipePath, () => resolve());
    });
  }

  /** 等首个通过鉴权的 agent；超时 → reject（调用方回退 mock） */
  waitHello() {
    if (this.hello) return Promise.resolve(this.hello);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this._helloWaiter = null; reject(new Error("agent handshake timeout")); }, this.handshakeMs);
      this._helloWaiter = (h, err) => { clearTimeout(timer); this._helloWaiter = null; err ? reject(err) : resolve(h); };
    });
  }

  _accept(sock) {
    let buf = ""; let authed = false;
    sock.setEncoding("utf8");
    sock.on("data", (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let f; try { f = JSON.parse(line); } catch { this.log.warn?.("agent frame not json"); continue; }
        if (!authed) {
          if (f.t === "hello" && typeof f.token === "string" && f.token.length === this.token.length
              && crypto.timingSafeEqual(Buffer.from(f.token), Buffer.from(this.token))) {
            authed = true; this.sock = sock;
            this.hello = { qq_version: String(f.qq_version || ""), pid: Number(f.pid || 0), api: String(f.api || "") };
            this.log.info?.({ pid: this.hello.pid, qq_version: this.hello.qq_version }, "[qqnt] agent attached");
            this._helloWaiter?.(this.hello);
          } else {
            this.log.warn?.("[qqnt] agent hello rejected");
            try { sock.destroy(); } catch {}
          }
          continue;
        }
        this._onFrame(f);
      }
    });
    sock.on("close", () => {
      if (this.sock === sock) {
        this.sock = null;
        for (const [, p] of this._pending) p.reject(new Error("agent disconnected"));
        this._pending.clear();
        this.evCb?.({ t: "agent_gone" });
      }
    });
    sock.on("error", () => {});
  }

  _onFrame(f) {
    if (f.t === "ret") {
      const p = this._pending.get(f.id);
      if (!p) return;
      this._pending.delete(f.id);
      clearTimeout(p.timer);
      f.ok ? p.resolve(f.r) : p.reject(new Error(String(f.e || "agent error")));
    } else if (f.t === "ev") {
      this.evCb?.(f.ev);
    }
  }

  onEvent(cb) { this.evCb = cb; }

  call(method, params, timeoutMs = 15000) {
    if (!this.sock) return Promise.reject(new Error("agent not attached"));
    const id = ++this._seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this._pending.delete(id); reject(new Error(`agent call timeout: ${method}`)); }, timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      this.sock.write(JSON.stringify({ id, t: "call", m: method, p: params ?? {} }) + "\n");
    });
  }

  async close() {
    this._closed = true;
    try { this.sock?.destroy(); } catch {}
    await new Promise((r) => { try { this._server?.close(() => r()); } catch { r(); } });
  }
}
