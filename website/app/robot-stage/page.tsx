import type { Metadata } from "next";
import RobotStage from "@/components/RobotStage";

/** IP 素材舞台（内部工具页）：不进搜索索引，正常访客不可达（无任何入口链接）。 */
export const metadata: Metadata = {
  title: "Robot Stage",
  robots: { index: false, follow: false },
};

export default function Page() {
  return <RobotStage />;
}
