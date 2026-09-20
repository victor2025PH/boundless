"use strict";

// 托管版后台令牌硬化（纯逻辑，main.js 启动期调用）。
//
// 背景：桌面壳与 sidecar 后端之间用 Bearer 令牌互认（config.backend.token →
// 启动时经 AITR_WEB_TOKEN 注入后端）。历史默认值 "admin" 对自建/开发态无所谓，
// 但托管版（卖给终端客户的成品）等于每台机器都用同一把公开钥匙守本机后台。
// 客户永远不需要知道这个令牌 → 首次托管启动时换成每机随机值即可，零交互。
//
// 何时换（shouldRotateToken）：仅托管版 && 令牌仍是出厂默认/空。已换过（非默认）
// 或用户显式自配的令牌绝不动。何时不换（调用方负责）：探测到后端已在跑——
// 它手里还是旧令牌，此刻换会让所有 Bearer 调用当场 401，下次冷启动再换。
const crypto = require("crypto");

const DEFAULT_TOKEN = "admin";

function shouldRotateToken(managed, token) {
  if (!managed) return false;
  const t = String(token == null ? "" : token).trim();
  return !t || t === DEFAULT_TOKEN;
}

/** 48 位十六进制随机令牌。rand 可注入（测试用），默认 crypto.randomBytes。 */
function generateToken(rand) {
  const bytes = (rand || crypto.randomBytes)(24);
  return Buffer.from(bytes).toString("hex");
}

module.exports = { DEFAULT_TOKEN, shouldRotateToken, generateToken };
