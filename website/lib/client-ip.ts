/**
 * 取真实客户端 IP —— 所有按 IP 计数的地方（限流、审计）都必须走这里。
 *
 * **坑**：`X-Forwarded-For` 是客户端可写的普通请求头。nginx 用
 * `$proxy_add_x_forwarded_for` 时语义是「**追加**」——客户端自己塞进来的值仍然留在
 * 最前面。所以 `xff.split(",")[0]` 取到的是**攻击者自己写的字符串**：每次请求换一个，
 * 任何按 IP 计数的东西就全废了（登录爆破限流、领取刷量限流都一样）。
 *
 * 正确做法是取**最后一跳**：那一段由我们自己的反代追加，客户端伪造不进去。
 * `TRUSTED_PROXY_HOPS` 用于 CDN → nginx 这类多层，往前多数几跳（默认 1）。
 *
 * ⚠️ 这一切的前提是流量**真的经过反代**。应用端口若对公网直接开放，任何请求头都不
 * 可信，代码层无解——那属于部署面：把 Next 绑到 127.0.0.1，或用防火墙挡掉应用端口。
 *
 * 另注：IP 从来只是辅助闸门。真正拦得住重复领取的是「按机器指纹幂等」，
 * 拦得住爆破的是账号级锁定；IP 轴用来抬高批量作业的成本，不是最后一道防线。
 */

const HOPS = Math.max(1, Number(process.env.TRUSTED_PROXY_HOPS || 1) || 1);

type HeaderBag = { headers: { get(name: string): string | null } };

export function clientIp(req: HeaderBag): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const parts = xff.split(",").map((s) => s.trim()).filter(Boolean);
    if (parts.length) {
      // 从右往左数 HOPS 跳；越界则取最左（比返回 unknown 更有信息量）
      const idx = Math.max(0, parts.length - HOPS);
      return parts[idx] || parts[parts.length - 1];
    }
  }
  const real = (req.headers.get("x-real-ip") || "").trim();
  return real || "unknown";
}
