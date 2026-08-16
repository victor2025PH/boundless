import type { Metadata } from "next";
import PaymentSettingsClient from "@/components/PaymentSettingsClient";

// 支付渠道设置（管理员）：/admin/payment。表单本体在 components/PaymentSettingsClient.tsx
// （客户端组件）；本文件保持服务端组件以便导出 metadata（后台页面绝不进搜索索引）。
export const metadata: Metadata = {
  title: "支付渠道设置 · 无界科技",
  robots: { index: false, follow: false },
};

export default function AdminPaymentPage() {
  return <PaymentSettingsClient />;
}
