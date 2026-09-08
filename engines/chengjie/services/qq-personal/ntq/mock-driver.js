/**
 * mock 驱动：离线自测 / CI / 端到端门禁用。实现 driver.js 的完整契约，不碰真实 QQ。
 *
 * 行为：
 *  - startLogin() 生成一张真二维码（qrcode 库画一个 x_qq_login://<nonce> 占位内容），
 *    QQ_MOCK_AUTOLOGIN_MS（默认 1500ms）后自动「扫码成功」→ selfInfo/loginState 变 logged_in，
 *    emit 一条 message_receive（模拟对端来消息），供端到端链路跑通。
 *  - 发送 / 撤回 / 已读 / 群管理：登了才成功，回递增 message_seq；未登录回 {ok:false, retcode:-403}。
 *  - QQ_MOCK_UIN / QQ_MOCK_NICK 可覆盖假账号。
 */
import QRCode from "qrcode";

export async function createMockDriver(opts = {}) {
  const log = (opts && opts.logger) || console;
  const uin = Number(process.env.QQ_MOCK_UIN || 10001);
  const nick = String(process.env.QQ_MOCK_NICK || "测试号");
  const autoMs = Number(process.env.QQ_MOCK_AUTOLOGIN_MS || 1500);
  let state = "logged_out";
  let seq = 5000;
  let evCb = null;
  let loginTimer = null;

  function emit(ev) {
    if (evCb) { try { evCb(ev); } catch (e) { log.debug?.({ e: String(e) }, "mock emit failed"); } }
  }

  return {
    __driverReason: "",
    kind: "mock",

    async info() {
      return { qq_installed: true, qq_version: "mock-9.9.21", driver: "mock",
               driver_reason: this.__driverReason };
    },
    loginState() { return state; },
    selfInfo() { return state === "logged_in" ? { uin, nickname: nick } : null; },

    async startLogin() {
      state = "qr_wait";
      const content = `x_qq_login://mock/${Date.now().toString(36)}`;
      const qr_png_base64 = (await QRCode.toDataURL(content, { margin: 1, width: 256 }))
        .replace(/^data:image\/png;base64,/, "");
      if (loginTimer) clearTimeout(loginTimer);
      loginTimer = setTimeout(() => {
        state = "logged_in";
        emit({ time: Math.floor(Date.now() / 1000), self_id: uin, event_type: "message_receive",
               data: { message_scene: "friend", peer_id: 42, sender_id: 42, message_seq: ++seq,
                       time: Math.floor(Date.now() / 1000),
                       segments: [{ type: "text", data: { text: "（mock）你好，这是一条演示入站消息" } }],
                       friend: { user_id: 42, nickname: "阿强", remark: "" } } });
      }, autoMs);
      return { ok: true, qr_png_base64, qr_url: content, expire_sec: 120 };
    },
    quickLoginList() { return []; },
    async quickLogin() { return { ok: false, error: "mock: no cached session" }; },
    async logout() { state = "logged_out"; return { ok: true }; },

    async getImplInfo() {
      return { impl_name: "ZhiliaoQQConnector(mock)", impl_version: "0.1.0",
               qq_version: "mock-9.9.21", milky_version: "1.3" };
    },

    _guard() { return state === "logged_in" ? null : { ok: false, error: "not logged in", retcode: -403 }; },

    async sendPrivate() { return this._guard() || { ok: true, message_seq: ++seq }; },
    async sendGroup() { return this._guard() || { ok: true, message_seq: ++seq }; },
    async recallPrivate() { return this._guard() || { ok: true }; },
    async recallGroup() { return this._guard() || { ok: true }; },
    async markRead() { return this._guard() || { ok: true }; },
    async uploadPrivateFile() { return this._guard() || { ok: true, file_id: "mockfile_" + (++seq) }; },
    async uploadGroupFile() { return this._guard() || { ok: true, file_id: "mockfile_" + (++seq) }; },
    async privateFileUrl() { return this._guard() || { ok: true, download_url: "https://mock.local/f.bin" }; },
    async groupFileUrl() { return this._guard() || { ok: true, download_url: "https://mock.local/f.bin" }; },
    async kickGroupMember() { return this._guard() || { ok: true }; },
    async setGroupName() { return this._guard() || { ok: true }; },
    async acceptFriend() { return this._guard() || { ok: true }; },

    onEvent(cb) { evCb = cb; },
    async stop() { if (loginTimer) clearTimeout(loginTimer); },
  };
}
