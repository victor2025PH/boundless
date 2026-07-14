# 深度诊断：解析链全链路测试
import sys, json
sys.path.insert(0, '.')

try:
    from src.chat.ai_client import ChatAI
    ai = ChatAI.__new__(ChatAI)
    ai._device_map = {"01": "192.168.1.5555", "02": "192.168.1.2555"}
    ai._defaults = {"target_country": "italy", "warmup_duration": 30}
    ai._sessions = {}

    test_msgs = [
        "运行土控上的手机，打开tiktok找到菲律宾女性20-25岁的活行互动，以及进入直播间评论互动",
        "菲律宾女性20-25岁直播间评论互动",
        "打开tiktok菲律宾直播互动，女性20-25岁",
        "所有手机进入菲律宾直播间，关注女性观众20-25岁",
        "菲律宾直播间评论互动女性20-25",
        "全部手机找菲律宾女生20-25，进直播间评论",
        "养号30分钟",  # 应该是warmup - 验证不误触发live
    ]

    print("=" * 70)
    bugs = []
    for msg in test_msgs:
        result = ai._multi_intent_parse(msg)
        intent = result.get("intent", "UNKNOWN")
        params = result.get("params", {})
        # 正确读取 target_country 的位置
        country = params.get("target_country", "")
        tc = result.get("targeting") or {}
        gender = tc.get("gender", "")
        age_min = tc.get("age_min", 0)
        age_max = tc.get("age_max", 0)

        is_live_bug = ("直播" in msg or "live" in msg.lower()) and intent == "warmup"
        is_ph_bug = "菲律宾" in msg and country != "philippines"
        is_age_bug = "20-25" in msg and (age_min == 0 or age_max == 0)
        is_gender_bug = ("女" in msg or "女性" in msg) and gender != "female"

        b_notes = []
        if is_live_bug: b_notes.append("直播→warmup误判")
        if is_ph_bug:   b_notes.append(f"菲律宾未提取(got:{country})")
        if is_age_bug:  b_notes.append(f"年龄未提取(got:{age_min}-{age_max})")
        if is_gender_bug: b_notes.append(f"性别未提取(got:{gender})")

        status = "BUG:" if b_notes else "OK "
        if b_notes: bugs.extend(b_notes)
        print(f"[{status}] {msg[:48]}")
        print(f"         intent={intent}, country={country}, gender={gender}, age={age_min}-{age_max}")
        if b_notes:
            for n in b_notes:
                print(f"         !!! {n}")
        print()

    print("=" * 70)
    print(f"发现 {len(bugs)} 个 Bug:")
    for b in bugs:
        print(f"  - {b}")
    if not bugs:
        print("  解析链路正常 - 问题在执行层或UI层")

    # 额外检查：_extract_country 是否能提取菲律宾
    print("\n--- _extract_country 单独测试 ---")
    test_country_msgs = ["菲律宾女性", "philippines women", "菲律宾", "Filipino girl"]
    for m in test_country_msgs:
        c = ai._extract_country(m)
        ok = "OK" if c == "philippines" else "BUG"
        print(f"[{ok}] '{m}' -> '{c}'")

except Exception as e:
    print("ERROR:", e)
    import traceback; traceback.print_exc()
