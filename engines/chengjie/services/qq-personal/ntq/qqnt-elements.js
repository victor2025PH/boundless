/**
 * Milky 段 ⇄ QQNT 内核消息元素（elements）转换——纯函数，无 IO。
 *
 * 移植自 LLOneBot v4.9.4 `ntqqapi/` 的元素形状（GPL-2.0，见 NOTICE.md）；智聊改动：
 * 只保留 Milky 用到的段（text / mention / mention_all / reply / image / face / file），
 * record（语音需 silk 转码）/ video / forward 在本版明确返回 unsupported 而不是静默丢。
 *
 * NT 元素类型号：1 text(含 @) · 2 pic · 3 file · 4 ptt · 5 video · 6 face · 7 reply
 * peer.chatType：1 好友 · 2 群 · 100 临时会话
 */

export const NT_ELEMENT = { TEXT: 1, PIC: 2, FILE: 3, PTT: 4, VIDEO: 5, FACE: 6, REPLY: 7 };
export const NT_CHAT_TYPE = { FRIEND: 1, GROUP: 2, TEMP: 100 };

export function scenePeer(scene, peerUid) {
  const chatType = scene === "group" ? NT_CHAT_TYPE.GROUP : scene === "temp" ? NT_CHAT_TYPE.TEMP : NT_CHAT_TYPE.FRIEND;
  return { chatType, peerUid: String(peerUid), guildId: "" };
}

/**
 * Milky OutgoingSegment[] → { elements, needs }：
 *  needs.uids  需要 uin→uid 解析的 @ 目标（agent 侧填 atNtUid）
 *  needs.files 需要先上传到 NT 富媒体缓存的本地/远程资源（image/file 段），元素里留占位 index
 * 不抛：不支持的段返回 { error }。
 */
export function milkyToElements(segs) {
  const elements = [];
  const needs = { uids: [], files: [] };
  for (const s of Array.isArray(segs) ? segs : []) {
    const t = String((s && s.type) || "");
    const d = (s && s.data) || {};
    if (t === "text") {
      elements.push({ elementType: NT_ELEMENT.TEXT, elementId: "",
        textElement: { content: String(d.text ?? ""), atType: 0, atUid: "", atTinyId: "", atNtUid: "" } });
    } else if (t === "mention") {
      const uin = String(d.user_id ?? "");
      needs.uids.push({ index: elements.length, uin });
      elements.push({ elementType: NT_ELEMENT.TEXT, elementId: "",
        textElement: { content: `@${uin}`, atType: 2, atUid: uin, atTinyId: "", atNtUid: "" } });
    } else if (t === "mention_all") {
      elements.push({ elementType: NT_ELEMENT.TEXT, elementId: "",
        textElement: { content: "@全体成员", atType: 1, atUid: "all", atTinyId: "", atNtUid: "all" } });
    } else if (t === "face") {
      elements.push({ elementType: NT_ELEMENT.FACE, elementId: "",
        faceElement: { faceIndex: Number(d.face_id || 0), faceType: 1 } });
    } else if (t === "reply") {
      elements.push({ elementType: NT_ELEMENT.REPLY, elementId: "",
        replyElement: { replayMsgSeq: String(d.message_seq ?? ""), replayMsgId: "", senderUin: "", senderUinStr: "" } });
    } else if (t === "image") {
      needs.files.push({ index: elements.length, kind: "image", uri: String(d.uri || ""),
        summary: String(d.summary || ""), sub_type: String(d.sub_type || "normal") });
      elements.push({ elementType: NT_ELEMENT.PIC, elementId: "", picElement: null });   // agent 上传后填
    } else if (t === "file") {
      needs.files.push({ index: elements.length, kind: "file", uri: String(d.uri || ""), name: String(d.name || "file") });
      elements.push({ elementType: NT_ELEMENT.FILE, elementId: "", fileElement: null });
    } else if (t === "record" || t === "video" || t === "forward") {
      return { error: `unsupported segment: ${t}` };
    } else {
      return { error: `unknown segment: ${t || "?"}` };
    }
  }
  if (!elements.length) return { error: "empty message" };
  return { elements, needs };
}

/** 图片元素填充（agent 上传到 NT 缓存后）：md5/size/尺寸/路径。picType 1000=jpg/png 2000=gif */
export function picElement(info, summary, subType) {
  return {
    md5HexStr: String(info.md5 || "").toLowerCase(), fileSize: String(info.size || 0),
    picWidth: Number(info.width || 0), picHeight: Number(info.height || 0),
    fileName: String(info.fileName || `${info.md5}.${info.ext || "jpg"}`),
    sourcePath: String(info.path || ""), original: true,
    picType: info.ext === "gif" ? 2000 : 1000, picSubType: subType === "sticker" ? 1 : 0,
    fileUuid: "", fileSubId: "", thumbFileSize: 0, summary: String(summary || ""),
  };
}

export function fileElement(info, name) {
  return {
    fileMd5: String(info.md5 || "").toLowerCase(), fileName: String(name || info.fileName || "file"),
    filePath: String(info.path || ""), fileSize: String(info.size || 0), picHeight: 0, picWidth: 0,
    picThumbPath: new Map(), file10MMd5: "", fileSha: "", fileSha3: "", fileUuid: "", fileSubId: "",
    thumbFileSize: 750, fileBizId: 0,
  };
}

/**
 * NT msgRecord → Milky IncomingMessage（message_receive.data）。
 * raw 里已有 senderUin / peerUin，不必再查 uid→uin；元素逐个转，认不出的转成 text 占位（不丢消息）。
 * 图片 temp_url 由 agent 补（需要 rkey），这里只放 resource_id。
 */
export function recordToMilky(rec, selfUin) {
  if (!rec || typeof rec !== "object") return null;
  const chatType = Number(rec.chatType || 0);
  const scene = chatType === NT_CHAT_TYPE.GROUP ? "group" : chatType === NT_CHAT_TYPE.TEMP ? "temp" : "friend";
  const peerUin = Number(rec.peerUin || 0);
  const senderUin = Number(rec.senderUin || 0);
  const segments = [];
  for (const el of Array.isArray(rec.elements) ? rec.elements : []) {
    const et = Number(el.elementType || 0);
    if (et === NT_ELEMENT.TEXT && el.textElement) {
      const te = el.textElement;
      if (Number(te.atType) === 1) segments.push({ type: "mention_all", data: {} });
      else if (Number(te.atType) === 2) segments.push({ type: "mention", data: { user_id: Number(te.atUid || 0) || 0 } });
      else segments.push({ type: "text", data: { text: String(te.content ?? "") } });
    } else if (et === NT_ELEMENT.PIC && el.picElement) {
      const pe = el.picElement;
      segments.push({ type: "image", data: {
        resource_id: String(pe.fileUuid || pe.md5HexStr || ""), temp_url: "",
        width: Number(pe.picWidth || 0), height: Number(pe.picHeight || 0),
        summary: String(pe.summary || ""), sub_type: Number(pe.picSubType) === 1 ? "sticker" : "normal",
        _nt: { originImageUrl: String(pe.originImageUrl || ""), md5: String(pe.md5HexStr || ""), fileName: String(pe.fileName || "") },
      } });
    } else if (et === NT_ELEMENT.FACE && el.faceElement) {
      segments.push({ type: "face", data: { face_id: Number(el.faceElement.faceIndex || 0) } });
    } else if (et === NT_ELEMENT.REPLY && el.replyElement) {
      segments.push({ type: "reply", data: { message_seq: Number(el.replyElement.replayMsgSeq || 0) } });
    } else if (et === NT_ELEMENT.FILE && el.fileElement) {
      const fe = el.fileElement;
      segments.push({ type: "file", data: { file_id: String(fe.fileUuid || fe.fileMd5 || ""), file_name: String(fe.fileName || ""),
        file_size: Number(fe.fileSize || 0), file_hash: String(fe.fileMd5 || "") } });
    } else if (et === NT_ELEMENT.PTT && el.pttElement) {
      segments.push({ type: "record", data: { resource_id: String(el.pttElement.fileUuid || el.pttElement.md5HexStr || ""), temp_url: "", duration: Number(el.pttElement.duration || 0) } });
    } else if (et === NT_ELEMENT.VIDEO && el.videoElement) {
      segments.push({ type: "video", data: { resource_id: String(el.videoElement.fileUuid || el.videoElement.videoMd5 || ""), temp_url: "" } });
    } else {
      segments.push({ type: "text", data: { text: `[unsupported element ${et}]` } });
    }
  }
  const data = {
    message_scene: scene, peer_id: scene === "group" ? Number(rec.peerUin || rec.peerUid || 0) : peerUin,
    sender_id: senderUin, message_seq: Number(rec.msgSeq || 0), time: Number(rec.msgTime || 0),
    segments, _nt: { msgId: String(rec.msgId || ""), peerUid: String(rec.peerUid || ""), senderUid: String(rec.senderUid || "") },
  };
  if (scene === "group") {
    data.group = { group_id: data.peer_id, group_name: String(rec.peerName || "") };
    data.group_member = { user_id: senderUin, nickname: String(rec.sendNickName || ""), card: String(rec.sendMemberName || rec.sendRemarkName || "") };
  } else {
    data.friend = { user_id: peerUin, nickname: String(rec.sendNickName || rec.peerName || ""), remark: String(rec.sendRemarkName || "") };
  }
  return { time: Number(rec.msgTime || 0), self_id: Number(selfUin || 0), event_type: "message_receive", data };
}

/** 用 PNG/JPEG/GIF 头解析尺寸（发图前填 picWidth/picHeight）；认不出 → {width:0,height:0,ext:""} */
export function imageMeta(buf) {
  if (!buf || buf.length < 10) return { width: 0, height: 0, ext: "" };
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf.length >= 24) {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20), ext: "png" };
  }
  if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46) {
    return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8), ext: "gif" };
  }
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    let i = 2;
    while (i + 9 < buf.length) {
      if (buf[i] !== 0xff) { i++; continue; }
      const marker = buf[i + 1];
      if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { i += 2; continue; }
      const len = buf.readUInt16BE(i + 2);
      if ((marker >= 0xc0 && marker <= 0xcf) && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7), ext: "jpg" };
      }
      i += 2 + len;
    }
    return { width: 0, height: 0, ext: "jpg" };
  }
  return { width: 0, height: 0, ext: "" };
}
