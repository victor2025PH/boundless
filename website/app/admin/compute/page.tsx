import type { Metadata } from "next";
import ComputeStatusClient from "@/components/ComputeStatusClient";

// 算力调度实时看板（管理员）：/admin/compute（实施70 2026-08-27 老板指令）。
// 数据由 117 集群 pusher 每 ~10s 推送到 /api/admin/compute-status，本页只读渲染；
// 鉴权与 /admin/payment 同款：admin_session cookie 直通，未登录落口令闸门。
export const metadata: Metadata = {
  title: "算力调度实时看板 | 管理后台",
  robots: { index: false, follow: false },
};

export default function AdminComputePage() {
  return <ComputeStatusClient />;
}
