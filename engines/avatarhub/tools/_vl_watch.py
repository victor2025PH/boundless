# -*- coding: utf-8 -*-
"""声纹锁实况监视：tail interp.err.log，把关键事件译成 ASCII 哨兵行(便于上层按行监听)。
VL-STEP=注册进度 VL-DONE=注册完成 VL-BLOCK=拦截(重置后不应再出现) VL-VETO=复核判旁人声撤回
GER-VETO=存疑撤回 用完 Ctrl+C 停。
"""
import io, os, re, sys, time

LOG = r"C:\模仿音色\logs\interp.err.log"
RULES = [
    (re.compile(r"声纹自动注册完成"), "VL-DONE"),
    (re.compile(r"声纹自动注册进度"), "VL-STEP"),
    (re.compile(r"声纹自动注册：段间相似度过低"), "VL-MIXED"),
    (re.compile(r"拦截非注册说话人"), "VL-BLOCK"),
    (re.compile(r"连拒自愈触发"), "VL-HEAL"),
    (re.compile(r"跳过与自家外放重叠的注册候选段"), "VL-SKIP"),
    (re.compile(r"复核=旁人声"), "VL-VETO"),
    (re.compile(r"存疑段复核=噪声,撤回"), "GER-VETO"),
    (re.compile(r"存疑段复核=真话,晋升"), "GER-REVIVE"),
]

def main():
    with io.open(LOG, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        sys.stdout.write("WATCHING\n"); sys.stdout.flush()
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            for pat, tag in RULES:
                if pat.search(line):
                    # 只保留时间戳与关键数字，避免控制台中文乱码干扰匹配
                    ts = line[:19]
                    sim = re.search(r"sim=([0-9.]+)", line)
                    step = re.search(r"(\d)/3", line)
                    extra = (" sim=" + sim.group(1) if sim else "") + \
                            (" step=" + step.group(1) if step else "")
                    sys.stdout.write(f"{tag}: {ts}{extra}\n"); sys.stdout.flush()
                    break

if __name__ == "__main__":
    main()
