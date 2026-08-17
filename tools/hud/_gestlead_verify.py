# -*- coding: utf-8 -*-
r"""C1 多目接力·指挥权仲裁——离线红绿验证器（2026-08-13）。

验什么：hud_server.gest_arbitrate(cands, cur, now)——多摄像头同时看到操作者时
「只允许一块屏响应手势」的指挥权仲裁纯函数。规格冻结出处：C1 多目接力·指挥权仲裁
（2026-08-13，hud_server.py 常量区注释）：
  GEST_FRESH_S = 1.5   候选新鲜窗：now - ts 超过即视为掉线
  GEST_KEEP    = 0.75  迟滞：在任领导者 q >= 最佳者 q*0.75 即保席位（防抖）
  规则：1) 只考虑新鲜候选（now - ts <= GEST_FRESH_S 且 q > 0）;
        2) 无新鲜候选 → 返回 ""（释放）;
        3) 在任者若是新鲜候选且 q >= 最佳者 q * GEST_KEEP → 连任;
        4) 否则由 q 最高的新鲜候选接任。

怎么跑（_ctrl_verify.py 同风格：直接跑、逐案 OK/FAIL、结尾统计；纯标准库、零副作用、
不起服务——hud_server 服务只在 __main__ 启动，import 是安全的）：
  D:\Miniconda3\python.exe D:\boundless\tools\hud\_gestlead_verify.py
退出码：0=全绿；1=有红；2=import hud_server 失败或 gest_arbitrate 尚未落盘。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 控制台是 GBK
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import hud_server as hs  # noqa: E402  服务只在 __main__ 启动，import 安全
except Exception as e:  # 缺依赖/环境不齐：人话退出，不甩裸 traceback
    print(f"[import失败] import hud_server 没成：{type(e).__name__}: {e}")
    print("人话：hud_server 引的是同仓 xiaojie_server/voice_gateway/ops_gateway，")
    print("      请在 D:\\boundless 树完整的机器上用 D:\\Miniconda3\\python.exe 跑。")
    sys.exit(2)

if not hasattr(hs, "gest_arbitrate"):
    print("主线尚未落盘，验证器就绪待跑（hud_server 里还没有 gest_arbitrate，落盘后重跑本文件）")
    sys.exit(2)

fails = []
total = [0]


def ck(name, cond):
    total[0] += 1
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = 1000.0   # 固定时钟：用例离线可复现，不取 time.time()


def C(q, age=0.0):
    """构造一个候选：q=手感质量，age=心跳距今秒数（>1.5 即过期）。"""
    return {"q": q, "ts": NOW - age}


arb = hs.gest_arbitrate

# ---- 用例0 常数对表（规格冻结 2026-08-13；运维若改档，下面的阈值用例须连带重标定）----
ck("用例0 常数对表：GEST_FRESH_S==1.5 且 GEST_KEEP==0.75",
   getattr(hs, "GEST_FRESH_S", None) == 1.5 and getattr(hs, "GEST_KEEP", None) == 0.75)

try:
    # 1. 单源上台
    ck("用例1 单源上台：空局面来一个 q=0.5 新鲜候选 → 接任该屏",
       arb({"s1": C(0.5)}, "", NOW) == "s1")

    # 2. 迟滞连任：0.6 >= 0.7*0.75(=0.525) → 在任者保席位
    ck("用例2 迟滞连任：在任0.6 vs 挑战0.7 → 在任者连任",
       arb({"a": C(0.6), "b": C(0.7)}, "a", NOW) == "a")

    # 3. 明显更强则接力：0.3 < 0.8*0.75(=0.6)
    ck("用例3 明显更强：在任0.3 vs 挑战0.8 → 挑战者接任",
       arb({"a": C(0.3), "b": C(0.8)}, "a", NOW) == "b")

    # 4. 在任者过期：心跳断流 2.0s（>1.5 窗），q 再高也不算候选
    ck("用例4 在任者过期：在任q=0.9但2.0s无心跳 vs 新鲜q=0.4 → 挑战者接任",
       arb({"a": C(0.9, age=2.0), "b": C(0.4)}, "a", NOW) == "b")

    # 5. 全部无手（q=0）→ 释放
    ck('用例5 全部无手：两路 q=0 皆新鲜 → 返回 ""（释放）',
       arb({"a": C(0.0), "b": C(0.0)}, "", NOW) == "")

    # 6. 全部过期 → 释放
    ck('用例6 全部过期：q>0 但心跳全超窗 → 返回 ""',
       arb({"a": C(0.9, age=3.0), "b": C(0.8, age=1.6)}, "a", NOW) == "")

    # 7. q=0 的在任者不算候选，不得靠迟滞霸座（q>0 才算候选）
    ck('用例7a 在任者手放下(q=0)且无他人 → 必须释放 ""（不得霸座）',
       arb({"a": C(0.0)}, "a", NOW) == "")
    ck("用例7b 在任者q=0 + 挑战者q=0.2 → 移交挑战者",
       arb({"a": C(0.0), "b": C(0.2)}, "a", NOW) == "b")

    # 8. 边界：恰好 q == 最佳*0.75 → 连任（>= 语义）。
    #    特意选二进制可精确表示的 0.5/0.375：0.5*0.75==0.375 无浮点舍入，边界判等真成立
    #    （0.6 vs 0.8 这类十进制数在 IEEE754 下 0.6 >= 0.8*0.75 是 False，会冤枉正确实现）。
    ck("用例8 边界：在任0.375 == 挑战0.5*0.75 → 在任者连任（>=语义）",
       arb({"a": C(0.375), "b": C(0.5)}, "a", NOW) == "a")

    # 9. 负向自检：判据改坏必红。GEST_KEEP 破坏成 0.0 → 任何在任者 q>=最佳*0 恒真
    #    → 用例3 的挑战者接不了任；恢复常数后复绿。顺带钉死「常数必须运行时可调」：
    #    若实现把 GEST_KEEP 烘进默认参数/闭包（打补丁失效），这里也会红。
    def _case3():
        return arb({"a": C(0.3), "b": C(0.8)}, "a", NOW)

    ok_good = _case3() == "b"
    _keep = hs.GEST_KEEP
    try:
        hs.GEST_KEEP = 0.0
        ok_broken = _case3() != "b"   # 行为确实变了：挑战者被迟滞挡在门外
    finally:
        hs.GEST_KEEP = _keep
    ok_restored = _case3() == "b"
    ck("用例9 负向自检：正确常数下用例3绿 / GEST_KEEP=0.0 时必红 / 恢复后复绿",
       ok_good and ok_broken and ok_restored)
except Exception as e:  # 用例中途炸=红，保住结尾统计与退出码
    ck(f"用例执行中途异常：{type(e).__name__}: {e}", False)

print("\n== _gestlead_verify:",
      f"ALL GREEN ({total[0]}/{total[0]})" if not fails else f"{len(fails)}/{total[0]} FAIL: {fails}",
      "==")
sys.exit(1 if fails else 0)
