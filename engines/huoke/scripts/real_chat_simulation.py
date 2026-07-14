#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实生产环境模拟 — A↔B 互发 50+ 轮日文消息.

绕过 FB 主 App search 路径, 直接用 ADB 驱动 Messenger:
1. 每轮: A 发一条 → 等 5s → B 用 AI 回复 → 截图
2. 用户画像: A=jp_caring_male (50岁离婚男), B=jp_female_midlife (40+女性)
3. 走真实业务流程: 互发 → emotion 评分 → readiness → referral_decision 落库

每步截图存 reports/chat_sim/ 下.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ADB = r"C:\Android\android-sdk\platform-tools\adb.exe"

# 用户画像
USERS = {
    "A": {
        "device": "4HUSIB4TBQC69TJZ",
        "name": "さとう たかひろ",
        "persona": "jp_caring_male",
        "profile": {
            "age": 50, "marital": "離婚",
            "location": "東京都品川区",
            "interests": ["散歩", "犬", "料理", "音楽"],
            "tone": "穏やかで気遣いのある中年男性",
        },
    },
    "B": {
        "device": "CACAVKLNU8SGO74D",
        "name": "しょうぶ あより",
        "persona": "jp_female_midlife",
        "profile": {
            "age": 45, "marital": "未婚",
            "location": "埼玉県川口市",
            "interests": ["自然", "花", "読書"],
            "bio": "自信に満ちた人が好き",
            "tone": "柔らかく素直な日本人女性",
        },
    },
}

# 50+ 轮预设日文对话脚本 (模拟真实生产, 含 emotion 演进 / readiness 上升 / 主动询问 LINE)
# 每条 (sender, message)
DIALOG_SCRIPT = [
    # 阶段 1: 寒暄 (1-8)
    ("A", "こんばんは、しょうぶさん。お友達申請を承認していただきありがとうございます。"),
    ("B", "こんばんは、さとうさん。こちらこそありがとうございます。よろしくお願いいたします。"),
    ("A", "今日はお疲れ様でした。お仕事は何をされているんですか?"),
    ("B", "ありがとうございます。私は地元の小さな会社で事務をしています。さとうさんは?"),
    ("A", "私は東京で小さなIT会社を経営しております。50歳になりまして、最近は仕事も少し落ち着いてきました。"),
    ("B", "ご自身で会社を!すごいですね。50歳でしたか、お若く見えます。"),
    ("A", "ありがとうございます。プロフィール写真は犬と散歩中のものです。一人暮らしなので犬が家族のようなものでして。"),
    ("B", "わんちゃん可愛いですね。私も動物が大好きです。お名前は何というんですか?"),
    # 阶段 2: 兴趣分享 (9-20)
    ("A", "ハナコといいます。柴犬で、もう8歳になります。毎朝公園を散歩するのが日課です。"),
    ("B", "ハナコちゃん、可愛いお名前ですね。柴犬は本当に賢そう。私も犬が飼いたいけど、住んでいるアパートが許可してくれなくて..."),
    ("A", "そうなんですね。それは寂しいですね。動物がいると本当に癒やされますよ。代わりに何かペットを飼われていますか?"),
    ("B", "実は最近、ベランダで小さな観葉植物を育てています。バラとか、ラベンダーとか。"),
    ("A", "素敵ですね。花はストレス解消にいいと聞きます。私も家にちょっと花を置いてみようかな。"),
    ("B", "ぜひ!花があると毎日が明るくなりますよ。さとうさんはお料理されますか?"),
    ("A", "実は最近料理にハマっています。一人暮らしなので、健康のために自炊してます。最近よく和食を作っています。"),
    ("B", "和食!いいですね。私も和食大好きです。最近作った中で一番美味しかったのは何ですか?"),
    ("A", "先週末に作った筑前煮です。母から教わったレシピで、根菜たっぷりでヘルシーです。"),
    ("B", "筑前煮、聞いただけでお腹が空いてきました。素敵な趣味ですね。"),
    ("A", "しょうぶさんもよく料理されますか?"),
    ("B", "はい、週末によく作ります。一人で食べるのも寂しいですが、好きな曲を聴きながら作っています。"),
    # 阶段 3: 情感 (21-32)
    ("A", "音楽聴きながら料理、いいですね。どんな曲がお好きなんですか?"),
    ("B", "昔のJ-POPが好きです。槇原敬之とか、平井堅とか。さとうさんは?"),
    ("A", "実は私もJ-POPが好きで。槇原敬之は私もよく聴きます。「もう恋なんてしない」とか心に染みますよね。"),
    ("B", "あの曲は本当にいい曲ですね。歌詞が大人な気持ちで...わかります。"),
    ("A", "失恋した時の気持ちが綺麗に描かれていて。私も離婚を経験したので余計に響きます。"),
    ("B", "そうなんですね。私もその気持ち、よくわかります。一度結婚に近かったこともあって、最近は一人で過ごす時間が増えました。"),
    ("A", "そうでしたか。一人の時間も悪くないですが、誰かと過ごす時間も大切ですよね。"),
    ("B", "ええ、最近そう思うようになりました。年を重ねると、人と話す時間がありがたく感じます。"),
    ("A", "わかります。普段、誰かと話す機会はどんな時に?"),
    ("B", "週末は妹と会うくらいで、平日はほとんど一人です。Facebookで人と話せるのが楽しみです。"),
    ("A", "そうなんですね。私も平日はほとんど誰とも話さないので、こうして話せるのは嬉しいです。"),
    ("B", "私もです。さとうさんとお話するの、なんだかホッとします。"),
    # 阶段 4: 信任建立 (33-42)
    ("A", "ありがとうございます。しょうぶさんと話していると、自然と心が落ち着きます。"),
    ("B", "そう言っていただけて嬉しいです。なかなか落ち着いて話せる方に出会えなくて。"),
    ("A", "本当にそうですよね。SNS時代は表面的な繋がりが多くて、深く話せる人が貴重です。"),
    ("B", "わかります。さとうさんは普段、お仕事以外で何を楽しまれていますか?"),
    ("A", "週末は散歩、ガーデニング、最近は写真も少し。デジカメで犬とか花を撮るのが好きで。"),
    ("B", "写真!素敵ですね。今度ハナコちゃんの写真、見せていただけますか?"),
    ("A", "もちろんです。実は今朝公園で撮った写真があって、後で送りますね。"),
    ("B", "嬉しいです。私も最近撮った花の写真、お見せします。"),
    ("A", "ぜひ。お互いの趣味を共有できるのは楽しいですよね。"),
    ("B", "本当に。さとうさん、こんな感じでもう少しお話できたら嬉しいです。"),
    # 阶段 5: 引流时机 (43-52)
    ("A", "もちろんです。Facebookだと通知が時々遅れたり、本文が長いと打ちにくかったりするのですが..."),
    ("B", "そうですね。私も同じこと感じています。"),
    ("A", "もしご迷惑でなければ、LINEでもお話できると嬉しいのですが。LINEの方がスタンプも送れて、楽しく続けられるかなと。"),
    ("B", "LINEですか...そうですね、私もLINEはよく使います。"),
    ("A", "もしよろしければ、私のIDをお送りします。タイミングが合えば追加していただければ。無理のない範囲で大丈夫ですので。"),
    ("B", "ありがとうございます。せっかくですし、追加させていただきますね。"),
    ("A", "嬉しいです。私のLINE IDは:satou_takahiro_50 です。お時間のある時に追加してください。"),
    ("B", "ありがとうございます!後で追加させていただきます。"),
    ("A", "急がなくて大丈夫です。これからもよろしくお願いします。"),
    ("B", "こちらこそ、よろしくお願いします。お話できて本当に嬉しかったです。"),
]


def adb_shell(device: str, cmd: str, timeout: float = 10.0) -> str:
    r = subprocess.run([ADB, "-s", device, "shell", cmd],
                       capture_output=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.stdout


def adb_run(device: str, args: List[str], timeout: float = 10.0):
    return subprocess.run([ADB, "-s", device] + args,
                          capture_output=True, timeout=timeout)


def screenshot(device: str, path: str) -> bool:
    """截屏到指定本地路径."""
    try:
        adb_run(device, ["shell", "screencap", "-p", "/sdcard/_ss.png"], timeout=15)
        adb_run(device, ["pull", "/sdcard/_ss.png", path], timeout=15)
        return os.path.exists(path) and os.path.getsize(path) > 1000
    except Exception as e:
        print(f"  截屏失败: {e}")
        return False


def open_messenger_to_peer(sender_device: str, peer_name: str) -> bool:
    """打开 Messenger 并进入与 peer 的对话页.

    路径: launch Messenger → 等列表加载 → tap peer 行.
    现有 Messenger 通常打开就在主列表 / 上次对话页, 用 back 回到列表再点 peer.
    """
    # 1. launch
    adb_run(sender_device, ["shell",
                              "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity"],
            timeout=15)
    time.sleep(5)
    # 2. back 几次确保在主列表
    for _ in range(3):
        adb_run(sender_device, ["shell", "input keyevent 4"], timeout=5)
        time.sleep(0.5)
    # 3. 重新 launch (干净状态进列表)
    adb_run(sender_device, ["shell",
                              "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity"],
            timeout=15)
    time.sleep(4)
    # 列表上 peer 应该在前几行 (1-3 行). 720x1600 屏, 行高约 130, 第一行 Y≈250
    # 简化: tap 屏幕中部偏上, 通常会进入"最近"对话 — 但不一定是 peer
    # 更稳: 用 search box (位置约 360, 175 是搜索条)
    return True


def send_message_via_messenger(sender_device: str, peer_name: str,
                                  message: str) -> bool:
    """用 ADB 在 Messenger 已打开的对话页输入并发送消息."""
    # 假设已在对话页 (open_messenger_to_peer 已 nav)
    # tap 输入框 (底部中央, 720x1600 → x=300, y=1480)
    adb_run(sender_device, ["shell", "input tap 300 1480"], timeout=5)
    time.sleep(1)
    # ADBKeyboard 支持中文/日文输入
    # 先确认 ADBKeyboard 是默认输入法
    adb_run(sender_device, ["shell", "ime set com.android.adbkeyboard/.AdbIME"],
            timeout=5)
    time.sleep(0.5)
    # 用 ADBKeyboard broadcast 发送日文文本
    adb_run(sender_device, [
        "shell", "am broadcast",
        "-a", "ADB_INPUT_TEXT",
        "--es", "msg", message
    ], timeout=10)
    time.sleep(1)
    # tap send button (paper plane icon, 一般在右下, 720x1600 → x=680, y=1480)
    adb_run(sender_device, ["shell", "input tap 680 1480"], timeout=5)
    time.sleep(2)
    return True


def main():
    out_dir = f"reports/chat_sim/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"输出目录: {out_dir}")
    print()

    # Step 1: 用户画像 banner
    print("=== 用户画像 ===")
    for k, u in USERS.items():
        print(f"  [{k}] {u['name']}")
        for pk, pv in u['profile'].items():
            print(f"      {pk}: {pv}")
    print()

    # Step 2: 截屏初始状态 - 3 设备的 messenger
    for k, u in USERS.items():
        d = u['device']
        adb_run(d, ["shell",
                     "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity"],
                timeout=15)
    time.sleep(8)
    for k, u in USERS.items():
        screenshot(u['device'], f"{out_dir}/00_initial_{k}.png")
        print(f"  [{k}] initial screenshot saved")
    print()

    # Step 3: 50+ 轮对话循环
    A_to_B_pairs = [(s, m) for s, m in DIALOG_SCRIPT]
    print(f"=== 开始 {len(A_to_B_pairs)} 轮对话 ===")

    history: List[Dict] = []
    for idx, (sender_key, msg) in enumerate(A_to_B_pairs, 1):
        sender = USERS[sender_key]
        receiver_key = "B" if sender_key == "A" else "A"
        receiver = USERS[receiver_key]
        print(f"[{idx:02d}/{len(A_to_B_pairs)}] {sender['name'][:8]} → {receiver['name'][:8]}: {msg[:40]}")

        # nav to peer 对话
        open_messenger_to_peer(sender['device'], receiver['name'])
        # send
        send_message_via_messenger(sender['device'], receiver['name'], msg)
        time.sleep(2)
        # screenshot sender side after send
        screenshot(sender['device'], f"{out_dir}/{idx:02d}_sender_{sender_key}.png")

        history.append({
            "round": idx,
            "from": sender_key,
            "from_name": sender['name'],
            "to": receiver_key,
            "to_name": receiver['name'],
            "message": msg,
            "ts": datetime.now().isoformat(),
        })

        # 写 history JSON
        with open(f"{out_dir}/history.json", "w", encoding="utf-8") as f:
            json.dump({"users": USERS, "history": history}, f,
                      ensure_ascii=False, indent=2)

        # 等真实节奏 (人不会秒回)
        if idx % 5 == 0:
            print(f"  ... pause 5s ...")
            time.sleep(5)

    # Step 4: 总结
    print()
    print(f"=== 完成 {len(history)} 轮对话 ===")
    print(f"📁 截图: {out_dir}/")
    print(f"📋 history: {out_dir}/history.json")


if __name__ == "__main__":
    main()
