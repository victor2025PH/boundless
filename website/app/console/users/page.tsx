// /console/users：控制台账号管理（仅 master）——列表 + 新建 + 改角色/禁用/重置密码。
// 写操作全部走 /api/console/users（master 校验、最后一个 enabled master 保护在 API 侧兜底）。
import { ShieldAlert } from "lucide-react";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { listUsers } from "@/lib/console-users";
import { NewUserForm, UserActions, type ConsoleUserItem } from "../ui";
import { Card, DataTable, EmptyState, PageHeader, RoleBadge, ShortId, Td, fmtDateTime } from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export default function UsersPage() {
  const me = getConsoleSessionUser();
  if (!me) return null;
  if (me.role !== "master") {
    return (
      <div className="mx-auto max-w-md py-16">
        <div className="rounded-xl border border-rose-500/30 bg-rose-950/20 p-5 text-center">
          <ShieldAlert className="mx-auto mb-2 h-8 w-8 text-rose-400" />
          <p className="text-sm font-semibold text-rose-300">仅 master 可管理用户</p>
          <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
            当前账号 {me.username}（{me.role}）无权访问本页。如需开通账号或调整角色，请联系主账号。
          </p>
        </div>
      </div>
    );
  }

  const users = listUsers() as ConsoleUserItem[];
  const enabledMasters = users.filter((u) => u.role === "master" && u.enabled).length;

  return (
    <div>
      <PageHeader
        title="用户"
        desc="控制台实名账号（users 表）：viewer 只读 / admin 可做客户与归属写操作 / master 额外管用户。禁用与重置密码会立即撤销该用户全部会话；最后一个启用的 master 不可禁用或降级。"
        actions={<NewUserForm />}
      />

      {users.length === 0 ? (
        <EmptyState title="还没有账号" hints={["理论上不可达：无账号时登录页会先走「初始化主账号」引导。"]} />
      ) : (
        <Card className="p-0">
          <DataTable head={["用户名", "角色", "状态", "创建时间", "最后登录", "操作"]}>
            {users.map((u) => {
              const isLastEnabledMaster = u.role === "master" && u.enabled && enabledMasters === 1;
              return (
                <tr key={u.id} className={`hover:bg-slate-800/40 ${u.enabled ? "" : "opacity-60"}`}>
                  <Td>
                    <span className="font-medium text-slate-200">{u.username}</span>
                    {u.id === me.userId && (
                      <span className="ml-1.5 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                        你
                      </span>
                    )}
                    {u.display_name && <span className="ml-1.5 text-xs text-slate-500">{u.display_name}</span>}
                    <div className="mt-0.5">
                      <ShortId id={u.id} />
                    </div>
                  </Td>
                  <Td>
                    <RoleBadge role={u.role} />
                  </Td>
                  <Td>
                    <span
                      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                        u.enabled
                          ? "border-emerald-500/30 bg-emerald-500/15 text-emerald-300"
                          : "border-rose-500/30 bg-rose-500/15 text-rose-300"
                      }`}
                    >
                      {u.enabled ? "启用" : "已禁用"}
                    </span>
                  </Td>
                  <Td className="text-xs text-slate-500">{fmtDateTime(u.created_at)}</Td>
                  <Td className="text-xs text-slate-500">{fmtDateTime(u.last_login)}</Td>
                  <Td>
                    <UserActions user={u} isLastEnabledMaster={isLastEnabledMaster} />
                  </Td>
                </tr>
              );
            })}
          </DataTable>
        </Card>
      )}

      <p className="mt-4 text-[11px] leading-relaxed text-slate-600">
        脚本/巡检可继续用 <code className="rounded bg-slate-800 px-1 py-0.5 font-mono text-amber-300/80">x-console-key</code>{" "}
        头（CONSOLE_KEY）调 /api/console/**，视为内置 master，不占用户表。所有账号与会话变更均写入审计流水。
      </p>
    </div>
  );
}
