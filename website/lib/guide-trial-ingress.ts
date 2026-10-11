/**
 * MatrixX 第一次真正发出消息后，只把 Telegram 用户名和功能 id 交到官网。
 * 客户端不带引擎密钥。密钥留在本进程，由 postGuideTrial 转给向导场。
 * 这扇门只收 matrixx。智聊桌面的用量仍走领取单，不从这里进。
 */

const USERNAME = /^[A-Za-z][A-Za-z0-9_]{4,31}$/;

export function guideTrialIngress(
  body: { telegram?: unknown; feature?: unknown } | null | undefined,
): { telegram: string; feature: string } | null {
  const feature = String(body?.feature || "").trim();
  if (feature !== "matrixx") return null;
  const telegram = String(body?.telegram || "").trim().replace(/^@+/, "");
  if (!USERNAME.test(telegram)) return null;
  return { telegram, feature };
}
