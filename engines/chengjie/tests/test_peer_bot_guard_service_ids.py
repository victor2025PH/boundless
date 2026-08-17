"""Telegram 平台固定「非人」id 名单的一致性门禁（2026-08-08）。

背景：`store.TELEGRAM_SERVICE_CHAT_KEYS` 的注释自称「全仓唯一一份名单」，而
`peer_bot_guard.TELEGRAM_KNOWN_SERVICE_BOT_IDS` 是事实上的第二份且已分叉——后者
当时只有 @BotFather，漏掉 777000（登录验证码）。分叉的代价是静默的：靠后者判
「非人」的消费方（主动触达候选过滤、unanswered_inbound 入站漏球巡检）会把官方
验证码会话当成「客户在等」，每次网页登录造一条假告警，而假告警是最快毁掉运维对
告警信任的东西。

两份名单**刻意不合并**（peer_bot_guard 是零 src 依赖的纯函数模块，在入站热路里被
加载，不该为去重把 8k 行的 store 拉进来），所以一致性只能由本门禁保证：store 那份
是 SSOT，peer_bot_guard 那份必须是它的超集。谁往 store 加新 id 而忘了同步，这里红。
"""
from src.inbox.peer_bot_guard import (
    TELEGRAM_KNOWN_SERVICE_BOT_IDS,
    conversation_row_is_bot,
)
from src.inbox.store import TELEGRAM_SERVICE_CHAT_KEYS


def test_guard_list_is_superset_of_store_ssot():
    missing = set(TELEGRAM_SERVICE_CHAT_KEYS) - set(TELEGRAM_KNOWN_SERVICE_BOT_IDS)
    assert not missing, (
        "store.TELEGRAM_SERVICE_CHAT_KEYS 新增了 %s 却没同步进 "
        "peer_bot_guard.TELEGRAM_KNOWN_SERVICE_BOT_IDS——这些 peer 会被当成真人，"
        "漏球巡检/主动触达都会对它们误动作" % sorted(missing))


def test_service_notification_peer_is_not_a_waiting_customer():
    """777000 的登录验证码不得被判成真人（这是本次真实假阳性的原型）。"""
    row = {
        "platform": "telegram",
        "chat_key": "777000",
        "chat_type": "private",   # 官方服务号就是 private，靠 chat_type 挡不住
        "username": "",           # 也不带 bot 后缀，靠 username 规则同样挡不住
        "peer_is_bot": 0,         # 没回过话 → 行为信号还没标记
        "name": "Telegram",
    }
    assert conversation_row_is_bot(row) is True


def test_human_peer_still_passes():
    """反向：普通真人不能被新名单误伤。"""
    row = {
        "platform": "telegram",
        "chat_key": "8142915241",
        "chat_type": "private",
        "username": "somebody",
        "peer_is_bot": 0,
    }
    assert conversation_row_is_bot(row) is False


def test_operator_override_still_wins():
    """运营显式覆写「确认真人」(-1) 优先于名单——尊重人的判断。"""
    row = {
        "platform": "telegram",
        "chat_key": "777000",
        "chat_type": "private",
        "peer_is_bot": -1,
    }
    assert conversation_row_is_bot(row) is False
