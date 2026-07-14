# -*- coding: utf-8 -*-
"""VN1 命名体系（2026-07-09）：为 voice_pack_aishell3 全库（精选 12 席之外的 206 人）
生成"去编号"的人设名 → 写 voice_pack_aishell3\\names.json（{spk: {title, note}}）。

设计（对应《角色库资源盘点_命名体系与三库分区三视角方案_20260709.md》§2）：
  · 名字 = 意象词（承载音高档）× 身份/动感词（承载风格），4~6 字，词库按
    性别(2) × 音高档(低音/中音/明亮/气声) × 风格(沉稳/自然/活泼) 分桶人工撰写；
  · 确定性分配：桶内按 spk 升序逐个取词（重跑结果一致，可回查可审阅）；
  · PIN 表钉死方案对照表里承诺过的名字（如 SSB0966→静水流深）；
  · note = "名字的特征"一句话：档位×风格×年龄口音 + 场景卖点 + 全库量化亮点，
    会被 _vp_rows() 用作卡片副行（scene 兜底），也是审阅命名合理性的依据；
  · 唯一性三重校验：桶间不重名、不撞精选名、不撞角色库保留名（温白开等）。

用法：.venv_launcher\\Scripts\\python.exe tools\\name_voice_pack.py [--dry]
  --dry 只打印不落盘。落盘后无需动 index.json（构建产物零污染）；
  删除 names.json 即回退编号自动名（_vp_rows 解析顺序天然兜底）。
"""
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VP = ROOT / "voice_pack_aishell3"
OUT = VP / "names.json"

# ── 与 avatar_hub.py 同源的档位口径（改这里必须同步改 hub，门禁 VN1 会对账）──
F0_CUTS = {"male": (139.0, 158.0), "female": (229.0, 252.0)}
AGE_CN = {"A": "少年", "B": "青年", "C": "中年", "D": "中老年"}
ACCENT_CN = {"north": "北方口音", "south": "南方口音", "others": "其他口音"}
SCENE = {
    ("低音", "沉稳"): "厚重可信，适合知识讲解、品牌口播",
    ("低音", "自然"): "低沉耐听，适合长时间直播",
    ("低音", "活泼"): "有磁性又带劲，适合带货控场",
    ("中音", "沉稳"): "平稳清晰，适合客服、产品解说",
    ("中音", "自然"): "百搭自然，日常对话首选",
    ("中音", "活泼"): "亲和有活力，适合互动闲聊",
    ("明亮", "沉稳"): "清亮利落，适合功能讲解",
    ("明亮", "自然"): "明快干净，适合有声书、口播",
    ("明亮", "活泼"): "高能有感染力，适合气氛带动",
}


def band_of(gender, f0):
    if not f0:
        return ""
    lo, hi = F0_CUTS.get(gender) or F0_CUTS["female"]
    return "低音" if f0 < lo else ("中音" if f0 <= hi else "明亮")


def style_of(iqr):
    if not iqr:
        return "自然"
    return "沉稳" if iqr < 39 else ("自然" if iqr <= 51 else "活泼")


# ── 方案对照表钉死的名字（这些 spk 在角色库里已有旧名，改名映射要与方案逐字一致）──
PINS = {
    "SSB0966": "静水流深",   # 现"清晰男声A"：低音+全库最稳梯队，不动声色的可靠讲述者
    "SSB0710": "爽朗掌柜",   # 现"清晰男声B"：男声偏亮+快语速，市井热络的招呼感
    "SSB1328": "深夜电台",   # 现"男声磁性"：低音+自然起伏，晚间陪伴感
}

# ── 角色库保留名（人设名/手工克隆名，命名表不得撞车）──
RESERVED = {
    "温白开", "铜钟低音",          # 本轮角色改名要用（内嵌声，无 spk 档案）
    "云帆", "晓桐", "林小玲", "京味幽默", "磁性港风", "沉稳港风", "清雅淑女",
    "硬汉先生", "欧美绅士", "阳光型男", "海外总裁", "张一健", "Inside",
}

# ── 词库：桶 = (性别, 音高档, 风格)；桶内顺序即分配顺序（前面的名字先用）──
POOLS = {
    # ═ 男声 ═
    ("male", "低音", "沉稳"): [
        "深谷回声", "墨色提琴", "岩底醇声", "古钟晚鸣", "沉檀慢语", "夜航灯塔",
        "厚土之声", "铜韵木纹", "老树年轮", "黑胶低回", "冬夜炉边", "石桥晚风",
        "幕后旁白", "松涛夜话", "静水流深",
    ],
    ("male", "低音", "自然"): ["深夜电台", "暖炉絮语", "老巷茶炉"],
    ("male", "低音", "活泼"): ["低音鼓点", "磁场控场"],
    ("male", "中音", "沉稳"): [
        "白衬衫顾问", "正午播报", "会议室定调", "青灰西装", "平湖秋月",
        "稳健台声", "卷宗与咖啡", "站台广播",
    ],
    ("male", "中音", "自然"): [
        "老友慢谈", "邻家兄长", "茶馆常客", "春山平话", "石板路慢行",
        "巷口棋局", "温热豆浆",
    ],
    ("male", "中音", "活泼"): ["球场解说", "巷口相声", "热汤沸腾", "集市掌柜", "篮球场边"],
    ("male", "明亮", "沉稳"): [
        "清泉石上", "晨光讲席", "白瓷茶盏", "山径向导", "竹影书声",
        "澄空快讯", "少年报幕",
    ],
    ("male", "明亮", "自然"): [
        "清朗晨风", "单车少年", "玻璃晴朗", "稻田口哨", "远足背包",
        "晨跑路线", "早读窗边",
    ],
    ("male", "明亮", "活泼"): ["开心茶馆", "阳台歌手", "雀跃鼓点", "口哨快板", "游园快闪"],
    ("male", "", "自然"): ["气声旁白"],
    # ═ 女声 ═
    ("female", "低音", "沉稳"): [
        "丝绒夜话", "蜜色黄昏", "檀香书房", "深巷茶馆", "暮色提琴", "月下低语",
        "琥珀暖光", "夜读人", "绒毯冬夜", "黛色远山", "旧钢琴曲", "缎面晚装",
        "石库门夜话", "微醺醇酒", "墨玉温润", "沉香慢火", "天鹅绒幕", "炉边小说",
        "晚祷钟声", "深蓝丝巾", "暗香疏影",
    ],
    ("female", "低音", "自然"): [
        "温热红茶", "云母微光", "栗色长发", "秋池静水", "软陶手作", "亚麻午后",
        "桂花温酒", "老唱片机", "苔色庭院", "焦糖布丁", "暖砂海岸", "木质书架",
        "烟雨渡口", "绒线围巾", "杏仁牛奶", "蜂蜜柚子茶", "陶壶煮茶", "暮云舒卷",
        "素锦晚晴", "深潭映月", "枫糖慢火", "银杏信笺", "灯下织毛衣", "湖畔木屋",
        "晚安故事",
    ],
    ("female", "低音", "活泼"): [
        "爵士酒馆", "红酒微醺", "摩登舞步", "篝火故事会", "深色玫瑰", "夜市烟火",
        "舞台追光", "波本糖果", "皮衣街拍", "午夜脱口秀", "炭火烤栗", "绛紫披肩",
        "鎏金晚宴", "黑咖啡加糖", "港湾汽笛", "弦上探戈", "山城火锅", "霓虹雨夜",
        "醒木一拍", "摇摆黑胶", "酒馆驻唱", "烈焰红唇",
    ],
    ("female", "中音", "沉稳"): [
        "图书馆之声", "主编来信", "素色套装", "讲堂粉笔", "台灯笔记", "云端客服",
        "平静湖面", "伴读白噪音", "简报时间", "檀色卷宗", "晨会纪要", "冷杉书桌",
        "直尺与笔", "空谷幽兰", "静好岁月", "展馆讲解员", "茶艺师", "蓝图与尺规",
    ],
    ("female", "中音", "自然"): [
        "棉麻日常", "午后温茶", "晒暖的被子", "邻家姐姐", "巷口花店", "温声细语",
        "米色毛衣", "春日厨房", "素颜清晨", "雨后阳台", "慢递情书", "布艺沙发",
        "豆浆油条", "藤椅轻摇", "麦色田埂", "晚风信箱", "糯米团子", "火车窗景",
        "皂角清香", "竹篮清晨", "蒲扇夏夜", "山间民宿", "手冲咖啡", "棉布围裙",
    ],
    ("female", "中音", "活泼"): [
        "汽水气泡", "糖炒栗子", "蹦跳马尾", "奶茶三分甜", "向日葵田", "周末游乐园",
        "铃铛口袋", "彩虹跳绳", "果酱吐司", "荔枝汽水", "春游巴士", "泡泡糖机",
        "街角快闪", "婚礼司仪", "集市吆喝", "啦啦队长", "溜冰场广播", "爆米花香",
        "小马过河", "樱桃发卡", "蜜桃冰沙", "转呼啦圈", "猜谜主持人", "抓娃娃机",
        "气球小贩", "郊游领队",
    ],
    ("female", "明亮", "沉稳"): [
        "水晶播报", "银铃朗读", "清溪讲解", "晨间新闻台", "冰川矿泉", "玉磬清音",
        "雪后初晴", "白鹭掠水", "风铃序曲", "澄澈天窗", "琉璃茶盏", "山泉煮雪",
        "晴空航站楼", "冰糖梨水", "望星少女",
    ],
    ("female", "明亮", "自然"): [
        "清甜柠檬水", "晨光竖琴", "青竹滴露", "雏菊信笺", "银杏大道", "苹果园晨读",
        "白纱窗帘", "泉水叮咚", "青梅煮雨", "玻璃风铃", "春笋新芽", "云雀清晨",
        "薄荷晨露", "溪畔朗读", "天光云影", "鸢尾花开", "早安广播站", "柑橘半岛",
        "风车山坡", "晴天晾衣绳", "樱花邮局",
    ],
    ("female", "明亮", "活泼"): [
        "元气马卡龙", "糖霜甜甜圈", "彩虹泡泡机", "蜜柑气泡水", "阳光啦啦操",
        "草莓摇滚", "篝火合唱", "蹦床云朵", "电台点歌台", "夏日水枪", "迪斯科灯球",
        "橘子汽水海", "跳格子冠军", "派对彩带", "棉花糖云", "风筝冲浪", "旋转木马",
        "快门连拍", "星星发卡", "气球贩卖机", "蜜瓜苏打", "舞台弹跳", "麦克风女孩",
        "尖叫过山车", "泼水节", "烟花大会", "糖果雨", "春日踏板车", "欢乐蹦蹦车",
    ],
    ("female", "", "自然"): ["气声絮语", "耳畔轻风", "羽毛耳语"],
}


def build_note(r, band, style, standout):
    """"名字的特征"一句话：档位×风格×年龄口音 + 场景卖点 + 全库量化亮点。"""
    sex = "男声" if r["gender"] == "male" else "女声"
    head = f"{band or '气声'}{style}·{AGE_CN.get(r.get('age') or '', '')}{sex}"
    acc = ACCENT_CN.get(r.get("accent") or "")
    if acc:
        head += f"·{acc}"
    scene = SCENE.get((band, style)) or ("F0 提取不到的气声质感，耳语感强" if not band
                                         else "自然真声，百搭好用")
    return f"{head}：{scene}" + (f"；{standout}" if standout else "")


def standouts(rows):
    """全库量化亮点：信噪比/语速/频宽全库前 12，音高同性别两端前 8。每人至多两条。"""
    n = 12
    top_snr = {r["spk"] for r in sorted(rows, key=lambda x: -(x.get("snr") or 0))[:n]}
    top_rate = {r["spk"] for r in sorted(rows, key=lambda x: -(x.get("rate") or 0))[:n]}
    top_bw = {r["spk"] for r in sorted(rows, key=lambda x: -(x.get("bw") or 0))[:n]}
    lows, highs = set(), set()
    for g in ("male", "female"):
        gs = [r for r in rows if r["gender"] == g and r.get("f0_med")]
        lows |= {r["spk"] for r in sorted(gs, key=lambda x: x["f0_med"])[:8]}
        highs |= {r["spk"] for r in sorted(gs, key=lambda x: -x["f0_med"])[:8]}
    out = {}
    for r in rows:
        tags = []
        if r["spk"] in top_snr:
            tags.append(f"棚级干净（信噪比 {r.get('snr'):.0f}dB 全库前 {n}）")
        if r["spk"] in top_rate:
            tags.append(f"快嘴梯队（{r.get('rate'):.1f} 字/秒全库前 {n}）")
        if r["spk"] in lows:
            tags.append("同性别最低音梯队")
        if r["spk"] in highs:
            tags.append("同性别最亮梯队")
        if r["spk"] in top_bw and len(tags) < 2:
            tags.append("频宽透亮（高频细节保留好）")
        out[r["spk"]] = "、".join(tags[:2])
    return out


def main():
    dry = "--dry" in sys.argv
    rows = json.loads((VP / "index.json").read_text(encoding="utf-8"))
    featured = {r["spk"]: r.get("title") or "" for r in
                json.loads((VP / "featured.json").read_text(encoding="utf-8"))}
    reserved = RESERVED | set(featured.values())

    # PIN 名不得在词库里重复占位 → 从池子里剔除后再分配
    pin_names = set(PINS.values())
    pools = {k: [t for t in v if t not in pin_names] for k, v in POOLS.items()}
    st = standouts(rows)

    names, used = {}, set()      # PIN 已从词库剔除，轮到本尊时正常入座
    cursor = {k: 0 for k in pools}
    shortfall = []
    for r in sorted(rows, key=lambda x: x["spk"]):     # spk 升序 → 确定性可重跑
        spk = r["spk"]
        if spk in featured:                            # 精选名优先级最高，命名表不覆盖
            continue
        band = band_of(r["gender"], r.get("f0_med"))
        style = style_of(r.get("f0_iqr"))
        if spk in PINS:
            title = PINS[spk]
        else:
            key = (r["gender"], band, style)
            pool = pools.get(key) or []
            i = cursor.get(key, 0)
            while i < len(pool) and (pool[i] in used or pool[i] in reserved):
                i += 1
            if i >= len(pool):
                shortfall.append((spk, key))
                continue
            title = pool[i]
            cursor[key] = i + 1
        assert title not in used, f"重名: {title}"
        assert title not in reserved or spk in PINS, f"撞保留名: {title}"
        assert not re.search(r"\d{3,}", title), f"人设名含编号: {title}"
        used.add(title)
        # note=完整"名字的特征"（tooltip/审阅用）；hl=量化亮点单列（hub 拼进卡片卖点行，
        # 与卡片既有的 性别/档位/年龄/口音 chips 不重复）
        names[spk] = {"title": title,
                      "note": build_note(r, band, style, st.get(spk, "")),
                      "hl": st.get(spk, "")}

    if shortfall:
        print("!! 词库不够用（补词后重跑）:")
        for spk, key in shortfall:
            print("  ", spk, key)
        sys.exit(2)

    total = len([r for r in rows if r["spk"] not in featured])
    print(f"生成 {len(names)}/{total} 条（精选 {len(featured)} 席保留不覆盖），重名 0。样例：")
    for spk in list(names)[:8] + list(names)[-4:]:
        print(f"  {spk}  {names[spk]['title']:<8} {names[spk]['note']}")
    if dry:
        print("(--dry 未落盘)")
        return
    OUT.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写 {OUT}（删除此文件即回退编号自动名）")


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
