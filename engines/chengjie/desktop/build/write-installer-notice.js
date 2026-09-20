"use strict";

// write-installer-notice.js —— 安装器「安装前须知」页：从人可编辑的双语文本源生成
// electron-builder 按语言自动挑选的 RTF（build/license_<lang>.rtf）。
//
// 为什么是 RTF、为什么要生成：
//   · NSIS MUI2 的 license 页是 RichEdit，只有 RTF 能做标题色/粗体/项目符号；
//     electron-builder 在 build.nsis.license 未设时会自动收集 build/license_<lang>.<ext>
//     生成 LicenseLangString（zh_CN → 2052 / en_US → 1033），中文用户看中文、英文
//     用户看英文，不再是一个文件里中英对照各占半屏。
//   · RTF 里非 ASCII 字符必须写成 \uN? 转义（RichEdit 对原始高字节按 ansicpg 解码，
//     手写中文 RTF 一保存就乱），所以由本脚本机械生成，人只改 .txt 源。
//   · 产物入库（不挂 predist 链——链已够长），门禁 uninstall-nsh-invariants 用源文件
//     sha1 核对 RTF 新鲜度：改了 .txt 忘了重跑本脚本 → 打包拒绝出生。
//
// 源文件标记（build/installer-notice.<lang>.txt，UTF-8）：
//   "# 标题"   小节标题（growth-700 粗体）      "- 条目"  项目符号（悬挂缩进）
//   "> 备注"   次级说明（gray-600）            空行      忽略（间距由段落属性控制）
//   其余行     正文段落（品牌墨色）
//
// 颜色来自 platform/brand/tokens.json：growth-700 #0B64B7（白底上 5.9:1，growth-500
// 在 9pt 小字只有 3.4:1 故不用）、ink #0B1020、gray-600 #4B5563。
//
// 用法：node build/write-installer-notice.js     （在 desktop/ 下）

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const buildDir = __dirname;

// [源, 产物, 默认字体表下标, ansi 代码页]
const TARGETS = [
  ["installer-notice.zh_CN.txt", "license_zh_CN.rtf", 0, 936],
  ["installer-notice.en_US.txt", "license_en_US.rtf", 1, 1252],
];

// 导出给门禁复用：源文本 sha1（统一 \n，避免编辑器 CRLF/LF 抖动导致假过期）
function sourceHash(text) {
  return crypto.createHash("sha1").update(text.replace(/\r\n/g, "\n"), "utf8").digest("hex");
}
exports.sourceHash = sourceHash;
exports.TARGETS = TARGETS;

function rtfEscape(s) {
  let out = "";
  for (const ch of s) {
    const cp = ch.codePointAt(0);
    if (ch === "\\" || ch === "{" || ch === "}") {
      out += "\\" + ch;
    } else if (cp < 0x80) {
      out += ch;
    } else if (cp <= 0xffff) {
      out += "\\u" + (cp > 0x7fff ? cp - 0x10000 : cp) + "?";
    } else {
      // 非 BMP：按 UTF-16 代理对各写一个 \u（RichEdit 支持）
      const hi = Math.floor((cp - 0x10000) / 0x400) + 0xd800;
      const lo = ((cp - 0x10000) % 0x400) + 0xdc00;
      out += "\\u" + (hi - 0x10000) + "?\\u" + (lo - 0x10000) + "?";
    }
  }
  return out;
}

function toRtf(text, fontIdx, codepage, hash) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const body = [];
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) continue;
    // 间距刻意压紧（sb100/sa30/sa40）：MUI 的 RichEdit 区约 7 行高，三节内容也得滚动，
    // 每多 1 行空白就是用户多滚一格
    if (line.startsWith("# ")) {
      body.push("\\pard\\sb100\\sa30\\b\\cf1\\fs20 " + rtfEscape(line.slice(2)) + "\\b0\\cf2\\fs18\\par");
    } else if (line.startsWith("- ")) {
      body.push("\\pard\\li300\\fi-300\\tx300\\sa20 \\u8226?\\tab " + rtfEscape(line.slice(2)) + "\\par");
    } else if (line.startsWith("> ")) {
      body.push("\\pard\\sa40\\cf3 " + rtfEscape(line.slice(2)) + "\\cf2\\par");
    } else {
      body.push("\\pard\\sa40 " + rtfEscape(line) + "\\par");
    }
  }
  return [
    "{\\rtf1\\ansi\\ansicpg" + codepage + "\\deff" + fontIdx + "\\uc1",
    "{\\fonttbl{\\f0\\fnil\\fcharset134 Microsoft YaHei UI;}{\\f1\\fnil\\fcharset0 Segoe UI;}}",
    // 1 = growth-700 标题色  2 = ink 正文  3 = gray-600 备注
    "{\\colortbl;\\red11\\green100\\blue183;\\red11\\green16\\blue32;\\red75\\green85\\blue99;}",
    // 门禁读取的源文本指纹（\* 使 RichEdit 忽略此未知目标）
    "{\\*\\cxsrc " + hash + "}",
    "\\f" + fontIdx + "\\fs18\\cf2",
    ...body,
    "}",
    "",
  ].join("\r\n");
}

function main() {
  for (const [srcName, outName, fontIdx, codepage] of TARGETS) {
    const srcPath = path.join(buildDir, srcName);
    const text = fs.readFileSync(srcPath, "utf8").replace(/^\uFEFF/, "");
    const rtf = toRtf(text, fontIdx, codepage, sourceHash(text));
    const outPath = path.join(buildDir, outName);
    fs.writeFileSync(outPath, rtf, "ascii");
    console.log("[write-installer-notice] " + outName + " <- " + srcName + " (" + rtf.length + " bytes)");
  }
}

if (require.main === module) main();
