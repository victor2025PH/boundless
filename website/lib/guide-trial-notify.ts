/**
 * 第一次真正用出结果时，把领取单上的 Telegram 用户名和产品 id 回传给向导场。
 * 只领取、还没消耗，不算做了这一项。
 * 密钥必须与引擎 inbox.cta.webhook_secret 相同。地址不填时走官网所在机器上
 * 已有的反向隧道（127.0.0.1:18799）。没配密钥就什么都不做。
 */

/** 引擎在 VPS 上的回环隧道，见 deploy/instances 的 ProdTunnel。 */
export const GUIDE_TRIAL_TUNNEL_URL = "http://127.0.0.1:18799/api/cta/convert";

type GuideTrialEnv = {
  GUIDE_TRIAL_URL?: string;
  GUIDE_TRIAL_SECRET?: string;
};

export function guideTrialTarget(
  env: GuideTrialEnv = process.env as GuideTrialEnv,
): { url: string; secret: string } | null {
  const secret = (env.GUIDE_TRIAL_SECRET || "").trim();
  if (!secret) return null;
  const url = (env.GUIDE_TRIAL_URL || GUIDE_TRIAL_TUNNEL_URL).trim();
  if (!url) return null;
  return { url, secret };
}

export type GuideTrialClaim = {
  contact?: string;
  contactKind?: string;
  product?: string;
  usedChars?: number;
};

export function guideTrialBody(
  claim: GuideTrialClaim,
  beforeUsed: number,
): { telegram: string; feature: string } | null {
  if (beforeUsed > 0) return null;
  if ((claim.usedChars || 0) <= 0) return null;
  if (claim.contactKind !== "telegram") return null;
  const feature = String(claim.product || "").trim();
  const telegram = String(claim.contact || "").trim();
  if (!feature || !telegram) return null;
  return { telegram, feature };
}

export async function postGuideTrial(
  body: { telegram: string; feature: string },
): Promise<void> {
  const target = guideTrialTarget();
  if (!target) return;
  const { url, secret } = target;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 2000);
  try {
    await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-cta-secret": secret,
      },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
  } catch {
    /* 用量上报不依赖这场记数 */
  } finally {
    clearTimeout(timer);
  }
}

export async function notifyGuideTrial(
  claim: GuideTrialClaim,
  beforeUsed: number,
): Promise<void> {
  const body = guideTrialBody(claim, beforeUsed);
  if (!body) return;
  await postGuideTrial(body);
}
