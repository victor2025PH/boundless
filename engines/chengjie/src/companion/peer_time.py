# -*- coding: utf-8 -*-
"""Q-29（#305 · 2RKH3H · 2026-09-12）：客户当地时间感知——已知城市即知时区，AI 不再问「你那边白天还是晚上」。

事故：十翼会话客户说过「我这边四点了，再一会天就亮了」，AI 40 分钟后说「你那边应该已经是晚上了吧」，
客户「你又记忆不行了」，再 40 分钟 AI 又问「你那边现在是白天还是晚上？」。代码事实：``time_context`` 只注入
**人设**当地时间，没有客户时区概念；``location`` 槽已 confirmed 但没人把城市换算成时区；「白天也上班，晚上…」
「四点了」这类时间叙述不是任何画像槽，抽取为零。

本模块三件事（纯函数 + 只读 / 单键 KV，任何失败回 None / ""，绝不抛）：

- **A 城市 → 时区**：:func:`resolve_peer_tz` —— ``location`` / ``residence`` confirmed 或 mentioned 的值
  （``profile_slots.cell_view`` 三形状）→ :data:`CITY_TZ`（≈250 城市 · 中 / 繁 / 英 / 日别名 + 单时区国家兜底）
  → IANA 时区；**命中不到返回 None，不猜**（多城市撞不同时区也 None——推断错比不推断更糟）。
  :func:`peer_local_time` → ``{hh_mm, hour, period: 早上/白天/傍晚/夜里, weekday}``。
- **C 时间叙述即事实**：:func:`detect_time_statement` —— 客户「我这边现在是晚上 / 刚天亮 / 现在四点了 /
  it's 4am here」→ ``{hour, period, match}``（只认对方说**自己**这边；「你那边应该是晚上了吧」不算）；
  :func:`note_inbound` 经 FactGate ``kind=transient``（原句锚定 + TTL 12h）写 InboxStore KV
  ``peer_time_hint:<conv>``；有钟点 → 反推 UTC 偏移（与城市表冲突以客户原话为准）。
- **B 注入装配**：:func:`build_peer_time_addendum` —— hint（12h 内）优先于城市推断；组 ``info`` 交
  ``time_context.build_peer_time_hint`` 纯格式化；tz 未知 → 「顺口问一次，24h 内不再问」（24h 内我方已问过
  → 附「HH:MM 已问过，本轮不要再问」）。接线：``excuse_budget.build_time_schedule_addendum``（Q-8 F 的
  ``persona_reply._prompt_addenda`` ④ try-block）尾部追加——**persona_reply.py / decide_probe_target 零改动**。

配置 ``companion.peer_time.enabled``：缺席按业务域（陪伴开 / 销售关），显式值优先。
日志 ``[peer-time] conv=… known=1 source=hint|city|hint+city tz=… local=HH:MM period=…`` /
``known=0 asked=HH:MM|-`` / ``hint conv=… hour=… period=… ttl=12h evidence=…`` / ``drop reason=…``。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("src.companion.peer_time")

CFG_PATH = "companion.peer_time"
HINT_KEY_PREFIX = "peer_time_hint:"
HINT_TTL_SEC = 12 * 3600.0
ASK_WINDOW_SEC = 24 * 3600.0
_ROWS_LIMIT = 40

PERIOD_MORNING = "早上"
PERIOD_DAY = "白天"
PERIOD_EVENING = "傍晚"
PERIOD_NIGHT = "夜里"
PERIODS = (PERIOD_MORNING, PERIOD_DAY, PERIOD_EVENING, PERIOD_NIGHT)
PERIOD_EN = {PERIOD_MORNING: "morning", PERIOD_DAY: "daytime", PERIOD_EVENING: "evening", PERIOD_NIGHT: "night"}

_WD_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WD_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def period_label(hour: Any) -> str:
    """小时 → 四段时段（与人设侧 ``time_context.daypart_label`` 七段刻意不同：对方时段只用来定
    「问候口径」，粗一点更稳）。"""
    try:
        h = int(hour) % 24
    except (TypeError, ValueError):
        return ""
    if 5 <= h < 11:
        return PERIOD_MORNING
    if 11 <= h < 17:
        return PERIOD_DAY
    if 17 <= h < 20:
        return PERIOD_EVENING
    return PERIOD_NIGHT


# ---------------------------------------------------------------------------
# A. 城市表（tz, 别名…）。别名：英文按词边界（casefold）；≤3 字母缩写区分大小写整词；汉字 / 假名子串。
# 刻意不收的歧义名：Birmingham / Portland / San Jose / Naples / Valencia / Santiago / Nice / Hamilton /
# Richmond / Columbus / Springfield / Jacksonville / Newcastle / Victoria / San Juan / Cambridge。
# ---------------------------------------------------------------------------
CITY_TZ: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # 东亚
    ("Asia/Tokyo", ("Tokyo", "东京", "東京", "トウキョウ")),
    ("Asia/Tokyo", ("Osaka", "大阪")),
    ("Asia/Tokyo", ("Nagoya", "名古屋")),
    ("Asia/Tokyo", ("Yokohama", "横滨", "横浜", "橫濱")),
    ("Asia/Tokyo", ("Sapporo", "札幌")),
    ("Asia/Tokyo", ("Fukuoka", "福冈", "福岡")),
    ("Asia/Tokyo", ("Kyoto", "京都")),
    ("Asia/Tokyo", ("Kobe", "神户", "神戸", "神戶")),
    ("Asia/Tokyo", ("Okinawa", "Naha", "冲绳", "沖縄", "沖繩", "那霸", "那覇")),
    ("Asia/Tokyo", ("Hiroshima", "广岛", "広島", "廣島")),
    ("Asia/Tokyo", ("Sendai", "仙台")),
    ("Asia/Seoul", ("Seoul", "首尔", "首爾", "ソウル", "서울")),
    ("Asia/Seoul", ("Busan", "釜山", "부산")),
    ("Asia/Seoul", ("Incheon", "仁川")),
    ("Asia/Seoul", ("Daegu", "大邱")),
    ("Asia/Shanghai", ("Beijing", "Peking", "北京")),
    ("Asia/Shanghai", ("Shanghai", "上海")),
    ("Asia/Shanghai", ("Guangzhou", "Canton", "广州", "廣州")),
    ("Asia/Shanghai", ("Shenzhen", "深圳")),
    ("Asia/Shanghai", ("Chengdu", "成都")),
    ("Asia/Shanghai", ("Chongqing", "重庆", "重慶")),
    ("Asia/Shanghai", ("Hangzhou", "杭州")),
    ("Asia/Shanghai", ("Wuhan", "武汉", "武漢")),
    ("Asia/Shanghai", ("Xi'an", "Xian", "西安")),
    ("Asia/Shanghai", ("Nanjing", "南京")),
    ("Asia/Shanghai", ("Tianjin", "天津")),
    ("Asia/Shanghai", ("Suzhou", "苏州", "蘇州")),
    ("Asia/Shanghai", ("Qingdao", "青岛", "青島")),
    ("Asia/Shanghai", ("Xiamen", "厦门", "廈門")),
    ("Asia/Shanghai", ("Changsha", "长沙", "長沙")),
    ("Asia/Shanghai", ("Zhengzhou", "郑州", "鄭州")),
    ("Asia/Shanghai", ("Kunming", "昆明")),
    ("Asia/Shanghai", ("Dalian", "大连", "大連")),
    ("Asia/Shanghai", ("Shenyang", "沈阳", "瀋陽")),
    ("Asia/Shanghai", ("Harbin", "哈尔滨", "哈爾濱")),
    ("Asia/Shanghai", ("Fuzhou", "福州")),
    ("Asia/Shanghai", ("Hefei", "合肥")),
    ("Asia/Shanghai", ("Jinan", "济南", "濟南")),
    ("Asia/Shanghai", ("Nanning", "南宁", "南寧")),
    ("Asia/Shanghai", ("Guiyang", "贵阳", "貴陽")),
    ("Asia/Shanghai", ("Dongguan", "东莞", "東莞")),
    ("Asia/Shanghai", ("Foshan", "佛山")),
    ("Asia/Shanghai", ("Ningbo", "宁波", "寧波")),
    ("Asia/Shanghai", ("Wenzhou", "温州", "溫州")),
    ("Asia/Shanghai", ("Shijiazhuang", "石家庄", "石家莊")),
    ("Asia/Shanghai", ("Nanchang", "南昌")),
    ("Asia/Shanghai", ("Changchun", "长春", "長春")),
    ("Asia/Shanghai", ("Taiyuan", "太原")),
    ("Asia/Shanghai", ("Lanzhou", "兰州", "蘭州")),
    ("Asia/Shanghai", ("Haikou", "海口")),
    ("Asia/Shanghai", ("Sanya", "三亚", "三亞")),
    ("Asia/Hong_Kong", ("Hong Kong", "Hongkong", "HK", "香港", "ホンコン")),
    ("Asia/Macau", ("Macau", "Macao", "澳门", "澳門")),
    ("Asia/Taipei", ("Taipei", "台北", "臺北")),
    ("Asia/Taipei", ("Kaohsiung", "高雄")),
    ("Asia/Taipei", ("Taichung", "台中", "臺中")),
    ("Asia/Taipei", ("Tainan", "台南", "臺南")),
    ("Asia/Taipei", ("Hsinchu", "新竹")),
    ("Asia/Taipei", ("Taoyuan", "桃园", "桃園")),
    ("Asia/Ulaanbaatar", ("Ulaanbaatar", "Ulan Bator", "乌兰巴托", "烏蘭巴托")),
    # 东南亚
    ("Asia/Bangkok", ("Bangkok", "曼谷", "バンコク")),
    ("Asia/Bangkok", ("Chiang Mai", "Chiangmai", "清迈", "清邁")),
    ("Asia/Bangkok", ("Phuket", "普吉", "普吉岛", "普吉島")),
    ("Asia/Bangkok", ("Pattaya", "芭提雅", "芭達雅")),
    ("Asia/Ho_Chi_Minh", ("Hanoi", "河内", "河內")),
    ("Asia/Ho_Chi_Minh", ("Ho Chi Minh", "Saigon", "HCMC", "胡志明", "西贡", "西貢")),
    ("Asia/Ho_Chi_Minh", ("Da Nang", "Danang", "岘港", "峴港")),
    ("Asia/Ho_Chi_Minh", ("Hai Phong", "Haiphong", "海防")),
    ("Asia/Phnom_Penh", ("Phnom Penh", "金边", "金邊")),
    ("Asia/Phnom_Penh", ("Siem Reap", "暹粒")),
    ("Asia/Vientiane", ("Vientiane", "万象", "萬象")),
    ("Asia/Yangon", ("Yangon", "Rangoon", "仰光")),
    ("Asia/Singapore", ("Singapore", "SG", "新加坡", "星加坡", "シンガポール")),
    ("Asia/Kuala_Lumpur", ("Kuala Lumpur", "KL", "吉隆坡")),
    ("Asia/Kuala_Lumpur", ("Penang", "George Town", "槟城", "檳城")),
    ("Asia/Kuala_Lumpur", ("Johor Bahru", "新山", "柔佛")),
    ("Asia/Kuala_Lumpur", ("Kota Kinabalu", "亚庇", "亞庇")),
    ("Asia/Kuala_Lumpur", ("Kuching", "古晋", "古晉")),
    ("Asia/Kuala_Lumpur", ("Ipoh", "怡保")),
    ("Asia/Jakarta", ("Jakarta", "雅加达", "雅加達")),
    ("Asia/Jakarta", ("Bandung", "万隆", "萬隆")),
    ("Asia/Jakarta", ("Surabaya", "泗水")),
    ("Asia/Jakarta", ("Medan", "棉兰", "棉蘭")),
    ("Asia/Makassar", ("Bali", "Denpasar", "巴厘岛", "巴厘島", "巴厘", "峇里")),
    ("Asia/Makassar", ("Makassar", "望加锡", "望加錫")),
    ("Asia/Manila", ("Manila", "马尼拉", "馬尼拉")),
    ("Asia/Manila", ("Cebu", "宿务", "宿霧")),
    ("Asia/Manila", ("Davao", "达沃", "達沃")),
    ("Asia/Manila", ("Quezon City", "奎松")),
    ("Asia/Brunei", ("Bandar Seri Begawan", "斯里巴加湾", "斯里巴加灣")),
    ("Asia/Dili", ("Dili", "帝力")),
    # 南亚
    ("Asia/Kolkata", ("New Delhi", "Delhi", "新德里", "德里")),
    ("Asia/Kolkata", ("Mumbai", "Bombay", "孟买", "孟買")),
    ("Asia/Kolkata", ("Bangalore", "Bengaluru", "班加罗尔", "班加羅爾")),
    ("Asia/Kolkata", ("Chennai", "金奈")),
    ("Asia/Kolkata", ("Kolkata", "Calcutta", "加尔各答", "加爾各答")),
    ("Asia/Kolkata", ("Hyderabad", "海得拉巴")),
    ("Asia/Kolkata", ("Pune", "浦那")),
    ("Asia/Karachi", ("Karachi", "卡拉奇")),
    ("Asia/Karachi", ("Lahore", "拉合尔", "拉合爾")),
    ("Asia/Karachi", ("Islamabad", "伊斯兰堡", "伊斯蘭堡")),
    ("Asia/Dhaka", ("Dhaka", "达卡", "達卡")),
    ("Asia/Colombo", ("Colombo", "科伦坡", "科倫坡")),
    ("Asia/Kathmandu", ("Kathmandu", "加德满都", "加德滿都")),
    ("Asia/Kabul", ("Kabul", "喀布尔", "喀布爾")),
    # 中亚 / 西亚
    ("Asia/Tashkent", ("Tashkent", "塔什干")),
    ("Asia/Almaty", ("Almaty", "阿拉木图", "阿拉木圖")),
    ("Asia/Tehran", ("Tehran", "德黑兰", "德黑蘭")),
    ("Asia/Baghdad", ("Baghdad", "巴格达", "巴格達")),
    ("Asia/Riyadh", ("Riyadh", "利雅得")),
    ("Asia/Riyadh", ("Jeddah", "吉达", "吉達")),
    ("Asia/Dubai", ("Dubai", "迪拜", "杜拜")),
    ("Asia/Dubai", ("Abu Dhabi", "阿布扎比")),
    ("Asia/Dubai", ("Sharjah", "沙迦")),
    ("Asia/Qatar", ("Doha", "多哈")),
    ("Asia/Kuwait", ("Kuwait City", "科威特城")),
    ("Asia/Bahrain", ("Manama", "麦纳麦", "麥納麥")),
    ("Asia/Muscat", ("Muscat", "马斯喀特", "馬斯喀特")),
    ("Asia/Amman", ("Amman", "安曼")),
    ("Asia/Beirut", ("Beirut", "贝鲁特", "貝魯特")),
    ("Asia/Damascus", ("Damascus", "大马士革", "大馬士革")),
    ("Asia/Jerusalem", ("Jerusalem", "耶路撒冷")),
    ("Asia/Jerusalem", ("Tel Aviv", "特拉维夫", "特拉維夫")),
    ("Europe/Istanbul", ("Istanbul", "伊斯坦布尔", "伊斯坦堡")),
    ("Europe/Istanbul", ("Ankara", "安卡拉")),
    ("Asia/Tbilisi", ("Tbilisi", "第比利斯")),
    ("Asia/Yerevan", ("Yerevan", "埃里温", "葉里溫")),
    ("Asia/Baku", ("Baku", "巴库", "巴庫")),
    # 欧洲
    ("Europe/London", ("London", "伦敦", "倫敦", "ロンドン")),
    ("Europe/London", ("Manchester", "曼彻斯特", "曼徹斯特")),
    ("Europe/London", ("Edinburgh", "爱丁堡", "愛丁堡")),
    ("Europe/London", ("Glasgow", "格拉斯哥")),
    ("Europe/London", ("Liverpool", "利物浦")),
    ("Europe/London", ("Leeds", "利兹", "利茲")),
    ("Europe/London", ("Bristol", "布里斯托")),
    ("Europe/Dublin", ("Dublin", "都柏林")),
    ("Europe/Lisbon", ("Lisbon", "Lisboa", "里斯本")),
    ("Europe/Lisbon", ("Porto", "波尔图", "波爾圖")),
    ("Europe/Madrid", ("Madrid", "马德里", "馬德里")),
    ("Europe/Madrid", ("Barcelona", "巴塞罗那", "巴塞隆納", "巴塞罗纳")),
    ("Europe/Madrid", ("Seville", "Sevilla", "塞维利亚", "塞維利亞")),
    ("Europe/Madrid", ("Malaga", "Málaga", "马拉加", "馬拉加")),
    ("Europe/Paris", ("Paris", "巴黎", "パリ")),
    ("Europe/Paris", ("Lyon", "里昂")),
    ("Europe/Paris", ("Marseille", "马赛", "馬賽")),
    ("Europe/Paris", ("Bordeaux", "波尔多", "波爾多")),
    ("Europe/Paris", ("Toulouse", "图卢兹", "圖盧茲")),
    ("Europe/Brussels", ("Brussels", "布鲁塞尔", "布魯塞爾")),
    ("Europe/Amsterdam", ("Amsterdam", "阿姆斯特丹")),
    ("Europe/Amsterdam", ("Rotterdam", "鹿特丹")),
    ("Europe/Berlin", ("Berlin", "柏林", "ベルリン")),
    ("Europe/Berlin", ("Munich", "München", "Munchen", "慕尼黑")),
    ("Europe/Berlin", ("Frankfurt", "法兰克福", "法蘭克福")),
    ("Europe/Berlin", ("Hamburg", "汉堡", "漢堡")),
    ("Europe/Berlin", ("Cologne", "Köln", "科隆")),
    ("Europe/Berlin", ("Düsseldorf", "Dusseldorf", "杜塞尔多夫", "杜塞道夫")),
    ("Europe/Berlin", ("Stuttgart", "斯图加特", "斯圖加特")),
    ("Europe/Zurich", ("Zurich", "Zürich", "苏黎世", "蘇黎世")),
    ("Europe/Zurich", ("Geneva", "Genève", "日内瓦", "日內瓦")),
    ("Europe/Vienna", ("Vienna", "Wien", "维也纳", "維也納")),
    ("Europe/Prague", ("Prague", "Praha", "布拉格")),
    ("Europe/Warsaw", ("Warsaw", "Warszawa", "华沙", "華沙")),
    ("Europe/Warsaw", ("Krakow", "Kraków", "克拉科夫")),
    ("Europe/Budapest", ("Budapest", "布达佩斯", "布達佩斯")),
    ("Europe/Rome", ("Rome", "Roma", "罗马", "羅馬", "ローマ")),
    ("Europe/Rome", ("Milan", "Milano", "米兰", "米蘭")),
    ("Europe/Rome", ("Turin", "Torino", "都灵", "都靈")),
    ("Europe/Rome", ("Florence", "Firenze", "佛罗伦萨", "佛羅倫斯", "翡冷翠")),
    ("Europe/Rome", ("Venice", "Venezia", "威尼斯")),
    ("Europe/Athens", ("Athens", "雅典")),
    ("Europe/Stockholm", ("Stockholm", "斯德哥尔摩", "斯德哥爾摩")),
    ("Europe/Oslo", ("Oslo", "奥斯陆", "奧斯陸")),
    ("Europe/Copenhagen", ("Copenhagen", "哥本哈根")),
    ("Europe/Helsinki", ("Helsinki", "赫尔辛基", "赫爾辛基")),
    ("Europe/Moscow", ("Moscow", "Москва", "莫斯科")),
    ("Europe/Moscow", ("Saint Petersburg", "St Petersburg", "St. Petersburg", "圣彼得堡", "聖彼得堡")),
    ("Europe/Kyiv", ("Kyiv", "Kiev", "基辅", "基輔")),
    ("Europe/Bucharest", ("Bucharest", "布加勒斯特")),
    ("Europe/Sofia", ("Sofia", "索菲亚", "索菲亞")),
    ("Europe/Belgrade", ("Belgrade", "贝尔格莱德", "貝爾格勒")),
    ("Europe/Zagreb", ("Zagreb", "萨格勒布", "薩格勒布")),
    ("Europe/Minsk", ("Minsk", "明斯克")),
    ("Europe/Vilnius", ("Vilnius", "维尔纽斯", "維爾紐斯")),
    ("Europe/Riga", ("Riga", "里加")),
    ("Europe/Tallinn", ("Tallinn", "塔林")),
    ("Atlantic/Reykjavik", ("Reykjavik", "雷克雅未克")),
    # 非洲
    ("Africa/Cairo", ("Cairo", "开罗", "開羅")),
    ("Africa/Lagos", ("Lagos", "拉各斯")),
    ("Africa/Lagos", ("Abuja", "阿布贾", "阿布賈")),
    ("Africa/Nairobi", ("Nairobi", "内罗毕", "奈洛比")),
    ("Africa/Johannesburg", ("Johannesburg", "Joburg", "约翰内斯堡", "約翰尼斯堡")),
    ("Africa/Johannesburg", ("Cape Town", "Capetown", "开普敦", "開普敦")),
    ("Africa/Johannesburg", ("Durban", "德班")),
    ("Africa/Casablanca", ("Casablanca", "卡萨布兰卡", "卡薩布蘭卡")),
    ("Africa/Accra", ("Accra", "阿克拉")),
    ("Africa/Addis_Ababa", ("Addis Ababa", "亚的斯亚贝巴", "阿迪斯阿貝巴")),
    ("Africa/Algiers", ("Algiers", "阿尔及尔", "阿爾及爾")),
    ("Africa/Tunis", ("Tunis", "突尼斯")),
    ("Africa/Dar_es_Salaam", ("Dar es Salaam", "达累斯萨拉姆", "達累斯薩拉姆")),
    ("Africa/Kampala", ("Kampala", "坎帕拉")),
    ("Africa/Kinshasa", ("Kinshasa", "金沙萨", "金夏沙")),
    ("Africa/Luanda", ("Luanda", "罗安达", "羅安達")),
    ("Africa/Khartoum", ("Khartoum", "喀土穆")),
    # 北美 · 美国
    ("America/New_York", ("New York", "NYC", "纽约", "紐約", "ニューヨーク")),
    ("America/New_York", ("Boston", "波士顿", "波士頓")),
    ("America/New_York", ("Philadelphia", "Philly", "费城", "費城")),
    ("America/New_York", ("Washington DC", "Washington D.C.", "Washington, DC", "华盛顿", "華盛頓")),
    ("America/New_York", ("Miami", "迈阿密", "邁阿密")),
    ("America/New_York", ("Orlando", "奥兰多", "奧蘭多")),
    ("America/New_York", ("Atlanta", "亚特兰大", "亞特蘭大")),
    ("America/New_York", ("Charlotte", "夏洛特")),
    ("America/New_York", ("Detroit", "底特律")),
    ("America/New_York", ("Pittsburgh", "匹兹堡", "匹茲堡")),
    ("America/New_York", ("Baltimore", "巴尔的摩", "巴爾的摩")),
    ("America/New_York", ("Tampa", "坦帕")),
    ("America/New_York", ("Buffalo", "布法罗", "水牛城")),
    ("America/New_York", ("Cleveland", "克利夫兰", "克里夫蘭")),
    ("America/Chicago", ("Chicago", "芝加哥", "シカゴ")),
    ("America/Chicago", ("Houston", "休斯顿", "休士頓", "休斯敦")),
    ("America/Chicago", ("Dallas", "达拉斯", "達拉斯")),
    ("America/Chicago", ("Austin", "奥斯汀", "奧斯汀")),
    ("America/Chicago", ("San Antonio", "圣安东尼奥", "聖安東尼奧")),
    ("America/Chicago", ("Minneapolis", "明尼阿波利斯")),
    ("America/Chicago", ("New Orleans", "新奥尔良", "紐奧良")),
    ("America/Chicago", ("Kansas City", "堪萨斯城", "堪薩斯城")),
    ("America/Chicago", ("Nashville", "纳什维尔", "納許維爾")),
    ("America/Chicago", ("Memphis", "孟菲斯")),
    ("America/Chicago", ("Milwaukee", "密尔沃基", "密爾瓦基")),
    ("America/Chicago", ("Oklahoma City", "俄克拉荷马城", "奧克拉荷馬市")),
    ("America/Chicago", ("St Louis", "St. Louis", "Saint Louis", "圣路易斯", "聖路易")),
    ("America/Denver", ("Denver", "丹佛")),
    ("America/Denver", ("Salt Lake City", "盐湖城", "鹽湖城")),
    ("America/Denver", ("Albuquerque", "阿尔伯克基", "阿布奎基")),
    ("America/Phoenix", ("Phoenix", "凤凰城", "鳳凰城")),
    ("America/Los_Angeles", ("Los Angeles", "LA", "洛杉矶", "洛杉磯", "ロサンゼルス")),
    ("America/Los_Angeles", ("San Francisco", "SF", "旧金山", "舊金山", "三藩市", "圣弗朗西斯科")),
    ("America/Los_Angeles", ("San Diego", "圣地亚哥", "聖地牙哥", "圣迭戈")),
    ("America/Los_Angeles", ("Seattle", "西雅图", "西雅圖")),
    ("America/Los_Angeles", ("Las Vegas", "Vegas", "拉斯维加斯", "拉斯維加斯")),
    ("America/Los_Angeles", ("Sacramento", "萨克拉门托", "沙加緬度")),
    ("America/Los_Angeles", ("Oakland", "奥克兰市", "屋崙")),
    ("America/Los_Angeles", ("Irvine", "尔湾", "爾灣")),
    ("America/Anchorage", ("Anchorage", "安克雷奇")),
    ("Pacific/Honolulu", ("Honolulu", "Hawaii", "檀香山", "夏威夷")),
    # 加拿大
    ("America/Toronto", ("Toronto", "多伦多", "多倫多")),
    ("America/Toronto", ("Ottawa", "渥太华", "渥太華")),
    ("America/Toronto", ("Montreal", "Montréal", "蒙特利尔", "蒙特婁", "满地可")),
    ("America/Toronto", ("Quebec City", "魁北克城")),
    ("America/Vancouver", ("Vancouver", "温哥华", "溫哥華")),
    ("America/Edmonton", ("Calgary", "卡尔加里", "卡加利")),
    ("America/Edmonton", ("Edmonton", "埃德蒙顿", "愛民頓")),
    ("America/Winnipeg", ("Winnipeg", "温尼伯", "溫尼伯")),
    ("America/Halifax", ("Halifax", "哈利法克斯")),
    # 墨西哥 / 中美 / 加勒比
    ("America/Mexico_City", ("Mexico City", "Ciudad de México", "墨西哥城")),
    ("America/Mexico_City", ("Guadalajara", "瓜达拉哈拉", "瓜達拉哈拉")),
    ("America/Mexico_City", ("Monterrey", "蒙特雷")),
    ("America/Cancun", ("Cancun", "Cancún", "坎昆")),
    ("America/Tijuana", ("Tijuana", "蒂华纳", "提華納")),
    ("America/Panama", ("Panama City", "巴拿马城", "巴拿馬城")),
    ("America/Costa_Rica", ("San José, Costa Rica", "圣何塞", "聖荷西")),
    ("America/Guatemala", ("Guatemala City", "危地马拉城", "瓜地馬拉市")),
    ("America/Havana", ("Havana", "La Habana", "哈瓦那")),
    ("America/Santo_Domingo", ("Santo Domingo", "圣多明各", "聖多明哥")),
    # 南美
    ("America/Bogota", ("Bogota", "Bogotá", "波哥大")),
    ("America/Bogota", ("Medellin", "Medellín", "麦德林", "麥德林")),
    ("America/Lima", ("Lima", "利马", "利馬")),
    ("America/Guayaquil", ("Quito", "基多")),
    ("America/Caracas", ("Caracas", "加拉加斯")),
    ("America/Argentina/Buenos_Aires", ("Buenos Aires", "布宜诺斯艾利斯", "布宜諾斯艾利斯")),
    ("America/Montevideo", ("Montevideo", "蒙得维的亚", "蒙特維多")),
    ("America/Sao_Paulo", ("Sao Paulo", "São Paulo", "圣保罗", "聖保羅")),
    ("America/Sao_Paulo", ("Rio de Janeiro", "Rio", "里约热内卢", "里約熱內盧", "里约")),
    ("America/Sao_Paulo", ("Brasilia", "Brasília", "巴西利亚", "巴西利亞")),
    ("America/La_Paz", ("La Paz", "拉巴斯")),
    ("America/Asuncion", ("Asuncion", "Asunción", "亚松森", "亞松森")),
    # 大洋洲
    ("Australia/Sydney", ("Sydney", "悉尼", "雪梨", "シドニー")),
    ("Australia/Sydney", ("Canberra", "堪培拉", "坎培拉")),
    ("Australia/Melbourne", ("Melbourne", "墨尔本", "墨爾本")),
    ("Australia/Brisbane", ("Brisbane", "布里斯班", "布里斯本")),
    ("Australia/Brisbane", ("Gold Coast", "黄金海岸", "黃金海岸")),
    ("Australia/Perth", ("Perth", "珀斯", "柏斯")),
    ("Australia/Adelaide", ("Adelaide", "阿德莱德", "阿德雷德")),
    ("Australia/Darwin", ("Darwin", "达尔文", "達爾文")),
    ("Australia/Hobart", ("Hobart", "霍巴特")),
    ("Pacific/Auckland", ("Auckland", "奥克兰", "奧克蘭")),
    ("Pacific/Auckland", ("Wellington", "惠灵顿", "威靈頓")),
    ("Pacific/Auckland", ("Christchurch", "基督城")),
    ("Pacific/Fiji", ("Suva", "Fiji", "苏瓦", "斐济", "斐濟")),
    ("Pacific/Port_Moresby", ("Port Moresby", "莫尔兹比港", "莫士比港")),
    ("Pacific/Guam", ("Guam", "关岛", "關島")),
)

#: 单时区国家兜底（城市没命中时才看）。跨多时区的 美国 / 加拿大 / 澳大利亚 / 巴西 / 墨西哥 / 俄罗斯 /
#: 印尼 刻意不收——「在美国」推不出时区，宁可 None。
COUNTRY_TZ: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Asia/Tokyo", ("Japan", "日本", "にほん")),
    ("Asia/Seoul", ("South Korea", "Korea", "韩国", "韓國", "南韩", "南韓")),
    ("Asia/Shanghai", ("China", "中国", "中國", "国内", "國內", "大陆", "大陸")),
    ("Asia/Taipei", ("Taiwan", "台湾", "臺灣", "台灣")),
    ("Asia/Bangkok", ("Thailand", "泰国", "泰國")),
    ("Asia/Ho_Chi_Minh", ("Vietnam", "Viet Nam", "越南")),
    ("Asia/Manila", ("Philippines", "菲律宾", "菲律賓")),
    ("Asia/Kuala_Lumpur", ("Malaysia", "马来西亚", "馬來西亞", "大马", "大馬")),
    ("Asia/Phnom_Penh", ("Cambodia", "柬埔寨")),
    ("Asia/Vientiane", ("Laos", "老挝", "寮國")),
    ("Asia/Yangon", ("Myanmar", "Burma", "缅甸", "緬甸")),
    ("Asia/Kolkata", ("India", "印度")),
    ("Asia/Karachi", ("Pakistan", "巴基斯坦")),
    ("Asia/Dhaka", ("Bangladesh", "孟加拉")),
    ("Asia/Colombo", ("Sri Lanka", "斯里兰卡", "斯里蘭卡")),
    ("Asia/Kathmandu", ("Nepal", "尼泊尔", "尼泊爾")),
    ("Asia/Dubai", ("UAE", "United Arab Emirates", "阿联酋", "阿聯")),
    ("Asia/Riyadh", ("Saudi Arabia", "沙特", "沙烏地")),
    ("Asia/Qatar", ("Qatar", "卡塔尔", "卡達")),
    ("Asia/Tehran", ("Iran", "伊朗")),
    ("Asia/Baghdad", ("Iraq", "伊拉克")),
    ("Asia/Jerusalem", ("Israel", "以色列")),
    ("Europe/Istanbul", ("Turkey", "Türkiye", "土耳其")),
    ("Europe/London", ("United Kingdom", "UK", "England", "Britain", "Great Britain", "Scotland", "Wales",
                       "英国", "英國", "英格兰", "英格蘭", "苏格兰", "蘇格蘭")),
    ("Europe/Dublin", ("Ireland", "爱尔兰", "愛爾蘭")),
    ("Europe/Paris", ("France", "法国", "法國")),
    ("Europe/Berlin", ("Germany", "Deutschland", "德国", "德國")),
    ("Europe/Rome", ("Italy", "Italia", "意大利", "義大利")),
    ("Europe/Madrid", ("Spain", "España", "西班牙")),
    ("Europe/Lisbon", ("Portugal", "葡萄牙")),
    ("Europe/Amsterdam", ("Netherlands", "Holland", "荷兰", "荷蘭")),
    ("Europe/Brussels", ("Belgium", "比利时", "比利時")),
    ("Europe/Zurich", ("Switzerland", "瑞士")),
    ("Europe/Vienna", ("Austria", "奥地利", "奧地利")),
    ("Europe/Warsaw", ("Poland", "波兰", "波蘭")),
    ("Europe/Prague", ("Czech", "Czechia", "捷克")),
    ("Europe/Budapest", ("Hungary", "匈牙利")),
    ("Europe/Stockholm", ("Sweden", "瑞典")),
    ("Europe/Oslo", ("Norway", "挪威")),
    ("Europe/Copenhagen", ("Denmark", "丹麦", "丹麥")),
    ("Europe/Helsinki", ("Finland", "芬兰", "芬蘭")),
    ("Europe/Athens", ("Greece", "希腊", "希臘")),
    ("Africa/Cairo", ("Egypt", "埃及")),
    ("Africa/Johannesburg", ("South Africa", "南非")),
    ("Africa/Lagos", ("Nigeria", "尼日利亚", "奈及利亞")),
    ("Africa/Nairobi", ("Kenya", "肯尼亚", "肯亞")),
    ("America/Argentina/Buenos_Aires", ("Argentina", "阿根廷")),
    ("America/Bogota", ("Colombia", "哥伦比亚", "哥倫比亞")),
    ("America/Lima", ("Peru", "秘鲁", "秘魯")),
    ("America/Caracas", ("Venezuela", "委内瑞拉", "委內瑞拉")),
    ("Pacific/Auckland", ("New Zealand", "NZ", "新西兰", "紐西蘭")),
    ("Asia/Singapore", ("Singapore",)),
)

# 汉字别名的「拆字」陷阱：加负向后顾，避免「北京都」「完成都」撞上 京都 / 成都。
_CJK_TRAP_LOOKBEHIND: Dict[str, str] = {
    "京都": r"(?<![北东東南西])",
    "成都": r"(?<![完达達变變赞讚])",
    "大连": r"(?<![一])",
    "大連": r"(?<![一])",
    "海口": r"(?<![上出入])",
    "台中": r"(?<![舞平])",
    "臺中": r"(?<![舞平])",
    "西安": r"(?<![陕陝])",
    "巴黎": r"",
    "德里": r"(?<![新])",
}
_CJK_ANY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0400-\u04ff]")


def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))


def _compile_rules(table: Tuple[Tuple[str, Tuple[str, ...]], ...]) -> Tuple[List[Tuple[Any, str, str, str]], Dict[str, Tuple[str, str]]]:
    """→ ``([(regex, tz, alias, kind)], {缩写: (tz, alias)})``；kind ∈ cjk|latin。"""
    rules: List[Tuple[Any, str, str, str]] = []
    abbr: Dict[str, Tuple[str, str]] = {}
    for tz, aliases in table:
        for a in aliases:
            a = _norm(a).strip()
            if not a:
                continue
            if _CJK_ANY_RE.search(a):
                pre = _CJK_TRAP_LOOKBEHIND.get(a, "")
                rules.append((re.compile(pre + re.escape(a)), tz, a, "cjk"))
            elif len(a) <= 3 and a.isalpha() and a.isupper():
                abbr[a] = (tz, a)
            else:
                rules.append((re.compile(r"(?<![a-z])" + re.escape(a.casefold()) + r"(?![a-z])"), tz, a, "latin"))
    # 长别名优先（「New York」先于「York」——本表不收 York，但保持稳定序）
    rules.sort(key=lambda r: -len(r[2]))
    return rules, abbr


_CITY_RULES, _CITY_ABBR = _compile_rules(CITY_TZ)
_COUNTRY_RULES, _COUNTRY_ABBR = _compile_rules(COUNTRY_TZ)
_TZ_OK: Dict[str, bool] = {}


def _tz_valid(name: str) -> bool:
    v = _TZ_OK.get(name)
    if v is None:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
            v = True
        except Exception:
            v = False
        _TZ_OK[name] = v
    return v


def _match_table(text: str, rules: List[Tuple[Any, str, str, str]], abbr: Dict[str, Tuple[str, str]],
                 *, free_text: bool = False) -> Dict[str, str]:
    """→ ``{tz: 命中别名}``（同 tz 多别名只留最长）。``free_text`` → 跳过人名形别名。"""
    s = _norm(text)
    low = s.casefold()
    hits: Dict[str, str] = {}
    for rx, tz, alias, kind in rules:
        if free_text and kind == "latin" and alias.casefold() in _NAME_LIKE:
            continue
        if rx.search(s if kind == "cjk" else low):
            if tz not in hits and _tz_valid(tz):
                hits[tz] = alias
    for a, (tz, alias) in abbr.items():
        if tz in hits:
            continue
        if re.search(r"(?<![A-Za-z])" + re.escape(a) + r"(?![A-Za-z])", s) and _tz_valid(tz):
            hits[tz] = alias
    return hits


def city_to_tz(text: Any, *, free_text: bool = False) -> Optional[str]:
    """城市 / 国家文本 → IANA 时区；命中不到或撞上多个不同时区 → None（不猜）。
    ``free_text=True``（扫整句而非槽值）→ 人名形别名（Austin / Charlotte…）不参与。"""
    s = _norm(text).strip()
    if not s or len(s) > 400:
        return None
    try:
        hits = _match_table(s, _CITY_RULES, _CITY_ABBR, free_text=free_text)
        if len(hits) == 1:
            return next(iter(hits))
        if len(hits) > 1:
            return None
        hits = _match_table(s, _COUNTRY_RULES, _COUNTRY_ABBR, free_text=free_text)
        if len(hits) == 1:
            return next(iter(hits))
    except Exception:
        return None
    return None


def city_label(text: Any, *, free_text: bool = False) -> str:
    """文本里命中的城市别名（给注入行「对方说过在 X」用）；无 → 原文截 20 字。"""
    s = _norm(text).strip()
    try:
        hits = (_match_table(s, _CITY_RULES, _CITY_ABBR, free_text=free_text)
                or _match_table(s, _COUNTRY_RULES, _COUNTRY_ABBR, free_text=free_text))
        if len(hits) == 1:
            return next(iter(hits.values()))
    except Exception:
        pass
    return s[:20]


def _fields_of(profile: Any) -> Dict[str, Any]:
    if not isinstance(profile, dict):
        return {}
    f = profile.get("fields")
    if isinstance(f, dict):
        return f
    return profile


def resolve_peer_place(profile: Any, *, history: Any = None) -> Optional[Tuple[str, str]]:
    """→ ``(tz, 城市标签)``；``location`` / ``residence`` confirmed 或 mentioned 的值查表，字段无值时扫
    ``history``（最近 30 条）里客户**自述坐标**的那条（``MENTION_LEXICON`` 命中 / 确定性抽取）。
    confirmed 优先于 mentioned；同级撞不同时区 → None。任何失败 None。"""
    try:
        from src.companion.goals.profile_slots import (
            SLOT_STATE_CONFIRMED, SLOT_STATE_MENTIONED, cell_view,
        )
    except Exception:
        return None
    cands: List[Tuple[int, str, str]] = []  # (rank, tz, label)
    fields = _fields_of(profile)
    try:
        for slot in ("location", "residence"):
            val, _src, st = cell_view(fields.get(slot)) if fields else ("", "", "unknown")
            if not val or st not in (SLOT_STATE_CONFIRMED, SLOT_STATE_MENTIONED):
                continue
            tz = city_to_tz(val)
            if tz:
                cands.append((0 if st == SLOT_STATE_CONFIRMED else 1, tz, city_label(val)))
    except Exception:
        cands = []
    if not cands and history:
        try:
            from src.companion.goals.profile_slots import _hist_items, _mention_hit, capture_from_text
            for d, t in reversed(_hist_items(history)):
                if d != "in":
                    continue
                tz, label = None, ""
                try:
                    for k, v in capture_from_text(t):
                        if k in ("location", "residence"):
                            tz = city_to_tz(v)
                            if tz:
                                label = city_label(v)
                                break
                except Exception:
                    tz = None
                if not tz and (_mention_hit(t, "location") or _mention_hit(t, "residence")):
                    tz = city_to_tz(t, free_text=True)
                    label = city_label(t, free_text=True) if tz else ""
                if tz:
                    cands.append((2, tz, label))
                    break
        except Exception:
            pass
    if not cands:
        return None
    best = min(r for r, _t, _l in cands)
    tzs = {tz: lb for r, tz, lb in cands if r == best}
    if len(tzs) != 1:
        return None
    tz, label = next(iter(tzs.items()))
    return tz, label


def resolve_peer_tz(profile: Any, *, history: Any = None) -> Optional[str]:
    """A 段主口：画像（``{fields}`` 或 fields 本身）→ IANA 时区；判不出 None。"""
    got = resolve_peer_place(profile, history=history)
    return got[0] if got else None


# ---------------------------------------------------------------------------
# 时区对象 / 对方当地时间
# ---------------------------------------------------------------------------
_OFFSET_RE = re.compile(r"^UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2})|\.(\d+))?$", re.I)


def _tzinfo(tz: Any) -> Optional[_dt.tzinfo]:
    if tz is None:
        return None
    if isinstance(tz, _dt.tzinfo):
        return tz
    name = str(tz or "").strip()
    if not name:
        return None
    m = _OFFSET_RE.match(name)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        if m.group(4):
            mins = int(round(float("0." + m.group(4)) * 60))
        try:
            return _dt.timezone(sign * _dt.timedelta(hours=hours, minutes=mins), name.upper().replace(" ", ""))
        except Exception:
            return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _as_aware(now: Any) -> _dt.datetime:
    if isinstance(now, _dt.datetime):
        return now if now.tzinfo is not None else now.astimezone()
    try:
        return _dt.datetime.fromtimestamp(float(now if now is not None else time.time()), tz=_dt.timezone.utc)
    except Exception:
        return _dt.datetime.now(tz=_dt.timezone.utc)


def peer_local_time(tz: Any, now: Any = None) -> Optional[Dict[str, Any]]:
    """tz（IANA 名 / ``UTC+8`` / tzinfo）+ now（epoch / datetime；None=此刻）→
    ``{tz, hh_mm, hour, minute, period, period_en, weekday, weekday_en, weekday_idx}``；tz 无效 None。"""
    tzi = _tzinfo(tz)
    if tzi is None:
        return None
    try:
        dt = _as_aware(now).astimezone(tzi)
    except Exception:
        return None
    p = period_label(dt.hour)
    return {
        "tz": str(tz) if not isinstance(tz, _dt.tzinfo) else (getattr(tz, "key", None) or str(tz)),
        "hh_mm": dt.strftime("%H:%M"), "hour": dt.hour, "minute": dt.minute,
        "period": p, "period_en": PERIOD_EN.get(p, ""),
        "weekday": _WD_ZH[dt.weekday()], "weekday_en": _WD_EN[dt.weekday()], "weekday_idx": dt.weekday(),
    }


def offset_from_statement(hour: int, minute: int, said_ts: float) -> str:
    """客户在 ``said_ts``（epoch）说「现在 hour:minute」→ 反推整点 UTC 偏移 ``UTC±N``。"""
    utc = _dt.datetime.fromtimestamp(float(said_ts), tz=_dt.timezone.utc)
    off = (int(hour) + int(minute or 0) / 60.0) - (utc.hour + utc.minute / 60.0)
    while off > 14:
        off -= 24
    while off <= -12:
        off += 24
    n = int(round(off))
    return f"UTC{'+' if n >= 0 else '-'}{abs(n)}"


def _tz_offset_hours(tz: Any, at: float) -> Optional[float]:
    tzi = _tzinfo(tz)
    if tzi is None:
        return None
    try:
        d = _dt.datetime.fromtimestamp(float(at), tz=tzi).utcoffset()
        return d.total_seconds() / 3600.0 if d is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# C. 时间叙述检出（只认对方说自己这边）
# ---------------------------------------------------------------------------
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str) -> Optional[int]:
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        d = _CN_DIGIT.get(s[1:]) if len(s) == 2 else None
        return 10 + d if d is not None else None
    if s.endswith("十") and len(s) == 2:
        d = _CN_DIGIT.get(s[0])
        return d * 10 if d is not None else None
    if len(s) == 3 and s[1] == "十":
        a, b = _CN_DIGIT.get(s[0]), _CN_DIGIT.get(s[2])
        return a * 10 + b if a is not None and b is not None else None
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


_ZH_PERIOD_WORDS = (
    ("凌晨", PERIOD_NIGHT), ("半夜", PERIOD_NIGHT), ("深夜", PERIOD_NIGHT), ("大半夜", PERIOD_NIGHT),
    ("午夜", PERIOD_NIGHT), ("夜里", PERIOD_NIGHT), ("夜裡", PERIOD_NIGHT), ("大晚上", PERIOD_NIGHT),
    ("晚上", PERIOD_NIGHT), ("天黑了", PERIOD_NIGHT), ("天还没亮", PERIOD_NIGHT), ("天還沒亮", PERIOD_NIGHT),
    ("天快亮", PERIOD_NIGHT), ("快天亮", PERIOD_NIGHT), ("天就亮", PERIOD_NIGHT), ("天要亮", PERIOD_NIGHT),
    ("刚天亮", PERIOD_MORNING), ("剛天亮", PERIOD_MORNING), ("天刚亮", PERIOD_MORNING), ("天剛亮", PERIOD_MORNING),
    ("天亮了", PERIOD_MORNING), ("大早上", PERIOD_MORNING), ("早上", PERIOD_MORNING), ("早晨", PERIOD_MORNING),
    ("清晨", PERIOD_MORNING), ("上午", PERIOD_MORNING), ("刚起床", PERIOD_MORNING), ("剛起床", PERIOD_MORNING),
    ("刚醒", PERIOD_MORNING), ("剛醒", PERIOD_MORNING),
    ("中午", PERIOD_DAY), ("下午", PERIOD_DAY), ("白天", PERIOD_DAY), ("大白天", PERIOD_DAY),
    ("傍晚", PERIOD_EVENING), ("黄昏", PERIOD_EVENING), ("黃昏", PERIOD_EVENING), ("太阳下山", PERIOD_EVENING),
    ("太陽下山", PERIOD_EVENING), ("天快黑", PERIOD_EVENING),
)
_ZH_PERIOD_ALT = "|".join(re.escape(w) for w, _p in sorted(_ZH_PERIOD_WORDS, key=lambda x: -len(x[0])))
_ZH_HERE = r"(?:我们这边|我們這邊|我们这里|我們這裡|我们这|我們這|我这边|我這邊|我这里|我這裡|我这儿|我這兒|我这|我這|这边|這邊|这里|這裡|这儿|這兒)"
_ZH_NOW = r"(?:现在|現在|都|已经|已經|才|刚|剛|这会|這會|这会儿|這會兒)"
_ZH_NUM = r"(?:\d{1,2}|[一二两兩三四五六七八九十〇零]{1,3})"
_ZH_CLOCK_RE = re.compile(
    rf"(?P<anchor>{_ZH_HERE})?\s*(?:{_ZH_NOW})*\s*(?:是)?\s*(?P<mod>{_ZH_PERIOD_ALT})?\s*(?P<h>{_ZH_NUM})\s*[点點]"
    rf"(?!\s*(?:都|也|儿|兒|点|點|不|没|沒|小|建议|建議|意见|意見|想法|要求|东西|東西|原因|问题|問題|理由|内容|內容|事|个|個))"
    rf"(?!\s*(?:半|多|钟|鐘|整)?\s*(?:见|見|开会|開會|出发|出發|再聊|联系|聯繫|之前|以前|以后|以後|之后|之後|开始|開始|结束|結束|"
    rf"上班|下班|起床|睡觉|睡覺|到|去|来|來|吃饭|吃飯|下课|下課|上课|上課|集合|准时|準時|左右吧))"
    rf"(?P<tail>半|多|钟|鐘|整|(?:{_ZH_NUM})\s*分?)?\s*(?P<le>了|啦|喽|嘍)?"
)
_ZH_PERIOD_RE = re.compile(
    rf"(?P<anchor>{_ZH_HERE})\s*(?:{_ZH_NOW})*\s*(?:是|都是|已经是|已經是|过|過|刚过|剛過|才)?\s*(?P<p>{_ZH_PERIOD_ALT})"
    rf"|(?P<now>现在|現在)\s*(?:是|都|已经|已經|都是|已经是|已經是|过|過|刚过|剛過)?\s*(?P<p2>{_ZH_PERIOD_ALT})"
    rf"|(?P<p3>刚天亮|剛天亮|天刚亮|天剛亮|天快亮|快天亮|天就亮|天要亮|天亮了|天黑了|太阳下山|太陽下山)"
)
_ZH_OTHER_RE = re.compile(r"你那边|你那邊|你那里|你那裡|你那儿|你那兒|你们那|你們那|你那|你现在|你現在|你这边|你這邊|您那|您现在|您現在")
_ZH_AM_HINT = re.compile(r"天亮|天就亮|快亮|起床|刚醒|剛醒|早上|早晨|早安|清晨|太阳出来|太陽出來|日出|上班路上|早饭|早餐|凌晨|半夜|"
                         r"睡不着|睡不著|失眠|还没睡|還沒睡|没睡|沒睡|通宵|熬夜|一夜")
_ZH_PM_HINT = re.compile(r"下班|晚饭|晚餐|吃晚|晚上|夜里|夜裡|入夜|天黑|睡|困|太阳下山|太陽下山|收工|下午|傍晚|晚安")
_ZH_Q_RE = re.compile(r"[?？]|(?:吗|嗎|么|麼)\s*[。.!！~～]*\s*$")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、；;]")
_ZH_NIGHT_SMALL = ("凌晨", "半夜", "深夜", "大半夜", "午夜")
_ZH_MORNING_WORDS = ("早上", "早晨", "清晨", "上午", "大早上", "刚天亮", "剛天亮", "天刚亮", "天剛亮", "天亮了", "刚起床", "剛起床", "刚醒", "剛醒")

_EN_HERE = r"(?:over here|here|on my (?:side|end)|where i am|my side|for me)"
_EN_PERIOD_WORDS = (
    ("early morning", PERIOD_MORNING), ("morning", PERIOD_MORNING), ("dawn", PERIOD_MORNING), ("sunrise", PERIOD_MORNING),
    ("sun is coming up", PERIOD_MORNING), ("sun's coming up", PERIOD_MORNING), ("just woke up", PERIOD_MORNING),
    ("noon", PERIOD_DAY), ("midday", PERIOD_DAY), ("afternoon", PERIOD_DAY), ("daytime", PERIOD_DAY), ("lunchtime", PERIOD_DAY),
    ("evening", PERIOD_EVENING), ("sunset", PERIOD_EVENING), ("dusk", PERIOD_EVENING), ("getting dark", PERIOD_EVENING),
    ("middle of the night", PERIOD_NIGHT), ("way past midnight", PERIOD_NIGHT), ("past midnight", PERIOD_NIGHT),
    ("midnight", PERIOD_NIGHT), ("late night", PERIOD_NIGHT), ("nighttime", PERIOD_NIGHT), ("night", PERIOD_NIGHT),
    ("super late", PERIOD_NIGHT), ("pretty late", PERIOD_NIGHT), ("really late", PERIOD_NIGHT), ("so late", PERIOD_NIGHT),
    ("late", PERIOD_NIGHT), ("dark", PERIOD_NIGHT), ("bedtime", PERIOD_NIGHT),
)
_EN_PERIOD_ALT = "|".join(re.escape(w) for w, _p in sorted(_EN_PERIOD_WORDS, key=lambda x: -len(x[0])))
_EN_ITS = r"(?:it'?s|it is|its)"
_EN_CLOCK_RE = re.compile(
    rf"(?:(?P<its>{_EN_ITS})\s+(?:already\s+|almost\s+|nearly\s+|like\s+|about\s+|around\s+|just\s+|past\s+)?)?"
    rf"(?P<h>\d{{1,2}})(?::(?P<m>\d{{2}}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?|in the morning|in the afternoon|in the evening|at night)?"
    rf"\s*(?P<anchor>{_EN_HERE}|already|now|rn|right now)?", re.I)
_EN_PERIOD_RE = re.compile(
    rf"(?:{_EN_ITS})\s+(?P<pre>already\s+|still\s+|almost\s+|getting\s+|the\s+)?(?P<p>{_EN_PERIOD_ALT})\s*(?P<anchor>{_EN_HERE}|already|now|rn|right now)?"
    rf"|(?P<p2>good morning|morning|evening|night)\s+(?P<anchor2>{_EN_HERE})"
    rf"|(?P<p3>just woke up|sun is coming up|sun's coming up|getting dark)", re.I)
_EN_OTHER_RE = re.compile(r"\bover there\b|\bthere\b(?!'s)|\byour (?:side|end|place|time|time ?zone)\b|\bfor you\b|\bwhere you are\b|\byou(?:'re| are)\b|\bu r\b", re.I)
_EN_PERIOD_SPECIFIC = ("midnight", "past midnight", "way past midnight", "middle of the night", "noon", "midday", "dawn", "sunrise", "sunset")
# 人名 / 泛词形别名：只在「画像槽值」精确查表时用，扫自由文本（history）时跳过，防「my friend Charlotte」撞城市
_NAME_LIKE = frozenset({"austin", "charlotte", "orlando", "darwin", "sofia", "florence", "phoenix", "lima", "rio",
                        "vegas", "hawaii", "fiji", "guam", "bali", "perth", "leeds", "bristol", "venice", "riga"})


def _lang_of(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", text) else "en"


def _clause_around(text: str, pos: int) -> str:
    start = 0
    for m in _CLAUSE_SPLIT_RE.finditer(text):
        if m.start() < pos:
            start = m.end()
        else:
            return text[start:m.start()]
    return text[start:]


def _period_for(word: str, lang: str) -> str:
    w = str(word or "").strip().lower()
    table = _ZH_PERIOD_WORDS if lang == "zh" else _EN_PERIOD_WORDS
    for k, p in table:
        if k.lower() == w:
            return p
    return ""


def _hour_24(h: Optional[int], mod_word: str, ap: str, sentence: str, lang: str) -> Tuple[Optional[int], bool]:
    """→ ``(24h 小时, 是否明确)``。上下午靠修饰词（凌晨 / 晚上 / 下午…）/ am-pm / 句内线索定；
    定不了 → ``(None, False)``（宁可不推时区，也不把「四点」猜成下午四点）。"""
    if h is None or h < 0 or h > 24:
        return None, False
    h = h % 24
    if ap:
        a = ap.lower().replace(".", "").replace(" ", "")
        if a in ("am", "inthemorning"):
            return (0 if h == 12 else h), True
        if a in ("pm", "intheafternoon", "intheevening", "atnight"):
            return (h if h >= 12 else h + 12), True
    if h > 12 or h == 0:
        return h, True
    w = str(mod_word or "").strip()
    if w:
        if w in _ZH_NIGHT_SMALL:
            return (h if h <= 6 else h + 12 if h < 12 else h), True
        if w in _ZH_MORNING_WORDS:
            return (0 if h == 12 else h), True
        if w == "白天":
            return (h + 12 if h <= 5 else h), True
        if w == "中午":
            return (12 if h == 12 else h + 12 if h <= 2 else h), True
        # 晚上 / 夜里 / 下午 / 傍晚 / 黄昏 … → 12 时制的下半天
        return (h if h >= 12 else h + 12), True
    if lang == "zh":
        am = bool(_ZH_AM_HINT.search(sentence))
        # 「还没睡」既含 AM 线索也含「睡」——先剔掉 AM 命中段再看 PM
        pm = bool(_ZH_PM_HINT.search(_ZH_AM_HINT.sub("", sentence)))
        if am and not pm:
            return (0 if h == 12 else h), True
        if pm and not am:
            return (h if h >= 12 else h + 12), True
    return None, False


def detect_time_statement(text: Any) -> Optional[Dict[str, Any]]:
    """客户说了自己这边的时间 / 时段 → ``{kind: clock|period, hour, minute, period, match, sentence}``；
    没说 / 说的是我方那边 / 是问句 → None。纯函数。"""
    orig = str(text or "").strip()
    raw = _norm(orig)
    if not raw or len(raw) > 2000:
        return None
    lang = _lang_of(raw)
    splitter = r"(?<=[。！？!?；;\n])\s*"
    # 匹配用 NFKC 归一（全角数字 / 标点），evidence 保留客户原句（FactGate 逐字锚定 + prompt 引用都用原文）
    sents_orig = [s for s in re.split(splitter, orig) if s and s.strip()]
    sents = [s for s in re.split(splitter, raw) if s and s.strip()]
    if len(sents_orig) != len(sents):
        sents_orig = sents
    best: Optional[Dict[str, Any]] = None
    for sent, sent_o in zip(sents, sents_orig):
        got = _detect_zh(sent) if lang == "zh" else _detect_en(sent)
        if got is None:
            continue
        got["sentence"] = sent_o.strip()[:120]
        if best is None or (got.get("hour") is not None and best.get("hour") is None):
            best = got
    return best


def _skip_clause_zh(clause: str) -> bool:
    return bool(_ZH_OTHER_RE.search(clause)) or bool(_ZH_Q_RE.search(clause.strip()))


def _skip_clause_en(clause: str) -> bool:
    return bool(_EN_OTHER_RE.search(clause)) or "?" in clause


def _detect_zh(sent: str) -> Optional[Dict[str, Any]]:
    for m in _ZH_CLOCK_RE.finditer(sent):
        clause = _clause_around(sent, m.start())
        if _skip_clause_zh(clause):
            continue
        h = _cn_num(m.group("h"))
        if h is None or h > 24:
            continue
        tail = m.group("tail") or ""
        le = m.group("le") or ""
        mod_word = m.group("mod") or ""
        # 「一点」几乎都是「一点点 / 有一点」——只有带 了 / 半 / 钟 / 修饰词时才是钟点
        if h == 1 and not (le or tail or mod_word):
            continue
        anchored = bool(m.group("anchor")) or bool(le) or bool(mod_word) or bool(tail) \
            or bool(re.search(r"现在|現在|已经|已經", clause))
        if not anchored:
            continue
        minute = 30 if tail.startswith("半") else 0
        if tail and tail not in ("半", "多", "钟", "鐘", "整"):
            mm = _cn_num(tail.replace("分", "").strip())
            minute = mm if mm is not None and mm < 60 else 0
        h24, sure = _hour_24(h, mod_word, "", sent, "zh")
        period = period_label(h24) if h24 is not None else _period_for(mod_word, "zh")
        return {"kind": "clock", "hour": h24 if sure else None, "hour_raw": h, "minute": minute,
                "period": period, "match": m.group(0).strip()[:40]}
    for m in _ZH_PERIOD_RE.finditer(sent):
        clause = _clause_around(sent, m.start())
        if _skip_clause_zh(clause):
            continue
        word = m.group("p") or m.group("p2") or m.group("p3") or ""
        p = _period_for(word, "zh")
        if not p:
            continue
        return {"kind": "period", "hour": None, "hour_raw": None, "minute": 0, "period": p, "match": m.group(0).strip()[:40]}
    return None


def _detect_en(sent: str) -> Optional[Dict[str, Any]]:
    low = sent.lower()
    for m in _EN_CLOCK_RE.finditer(low):
        # 「it's 4 here / it's 4am / 4am here」认；裸「3 here」「at 3pm」（约时间）不认
        if not (m.group("its") or (m.group("anchor") and m.group("ap"))):
            continue
        clause = _clause_around(low, m.start())
        if _skip_clause_en(clause):
            continue
        try:
            h = int(m.group("h"))
        except (TypeError, ValueError):
            continue
        if h > 24:
            continue
        minute = int(m.group("m") or 0)
        if minute >= 60:
            minute = 0
        h24, sure = _hour_24(h, "", m.group("ap") or "", low, "en")
        if h24 is None and not m.group("ap"):
            # 「it's 4 here」上下午未明：句内 late / night / morning 线索
            p_hint = ""
            for k, p in _EN_PERIOD_WORDS:
                if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low):
                    p_hint = p
                    break
            if p_hint == PERIOD_MORNING or (p_hint == PERIOD_NIGHT and h <= 5):
                h24, sure = (0 if h == 12 else h), True
            elif p_hint in (PERIOD_DAY, PERIOD_EVENING, PERIOD_NIGHT):
                h24, sure = (h if h >= 12 else h + 12), True
        period = period_label(h24) if h24 is not None else ""
        return {"kind": "clock", "hour": h24 if sure else None, "hour_raw": h, "minute": minute,
                "period": period, "match": m.group(0).strip()[:40]}
    for m in _EN_PERIOD_RE.finditer(low):
        clause = _clause_around(low, m.start())
        if _skip_clause_en(clause):
            continue
        word = (m.group("p") or m.group("p2") or m.group("p3") or "").strip()
        if m.group("p"):
            pre = (m.group("pre") or "").strip()
            if not m.group("anchor") and pre not in ("already", "still") and word not in _EN_PERIOD_SPECIFIC:
                # 「it's late」无 here / now / already 锚 → 太泛（"it's late for dinner"），不认
                continue
        p = _period_for(word.replace("good ", ""), "en")
        if not p:
            continue
        return {"kind": "period", "hour": None, "hour_raw": None, "minute": 0, "period": p, "match": m.group(0).strip()[:40]}
    return None


# ---------------------------------------------------------------------------
# hint KV（FactGate kind=transient · TTL 12h）
# ---------------------------------------------------------------------------

def hint_key(conversation_id: str) -> str:
    return f"{HINT_KEY_PREFIX}{str(conversation_id or '').strip()}"


def _store(inbox_store: Any) -> Any:
    if inbox_store is not None:
        return inbox_store
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def read_hint(inbox_store: Any, conversation_id: str, *, now: Optional[float] = None,
              ttl_sec: float = HINT_TTL_SEC) -> Optional[Dict[str, Any]]:
    """KV 里的 hint（未过期）→ dict；无 / 过期 / 坏值 → None。"""
    st = _store(inbox_store)
    cid = str(conversation_id or "").strip()
    if st is None or not cid or not hasattr(st, "get_app_setting"):
        return None
    try:
        raw = st.get_app_setting(hint_key(cid), "") or ""
        if not raw:
            return None
        h = json.loads(raw)
        if not isinstance(h, dict):
            return None
        ts = float(h.get("ts") or 0)
        n = float(now if now is not None else time.time())
        if ts <= 0 or n - ts > float(h.get("ttl_sec") or ttl_sec):
            return None
        return h
    except Exception:
        return None


def _write_hint(inbox_store: Any, conversation_id: str, hint: Dict[str, Any]) -> bool:
    st = _store(inbox_store)
    if st is None or not hasattr(st, "set_app_setting"):
        return False
    try:
        payload = json.dumps(hint, ensure_ascii=False)
        try:
            st.set_app_setting(hint_key(conversation_id), payload, updated_by="peer_time")
        except TypeError:
            st.set_app_setting(hint_key(conversation_id), payload)
        return True
    except Exception:
        logger.debug("[peer-time] write hint failed", exc_info=True)
        return False


def note_inbound(text: Any, conversation_id: str, *, inbox_store: Any = None, ts: Optional[float] = None,
                 city_tz: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """客户一条入站 → 检出时间叙述 → FactGate ``kind=transient``（原句锚定）→ 写 KV（TTL 12h）。
    返回写入的 hint；没说 / 门不过 / 更旧于已存 → None。绝不抛。"""
    cid = str(conversation_id or "").strip()
    try:
        got = detect_time_statement(text)
        if got is None:
            return None
        t = float(ts if ts is not None else time.time())
        try:
            from src.companion import fact_gate
            ok, why = fact_gate.check(got["match"], slot_or_kind=fact_gate.KIND_TRANSIENT, evidence=got["sentence"],
                                      inbound_texts=[{"direction": "in", "text": str(text or "")}])
        except Exception:
            ok, why = False, "gate_error"
        if not ok:
            logger.info("[peer-time] drop conv=%s value=%r reason=%s", cid or "-", got["match"], why)
            return None
        hint: Dict[str, Any] = {
            "kind": got["kind"], "hour": got.get("hour"), "hour_raw": got.get("hour_raw"),
            "minute": int(got.get("minute") or 0), "period": got.get("period") or "",
            "match": got["match"], "evidence": got["sentence"][:120], "ts": t, "ttl_sec": HINT_TTL_SEC,
            "tz_guess": "",
        }
        if hint["hour"] is None and got.get("hour_raw") is not None and city_tz:
            # 上下午未明但城市时区已知 → 取与城市钟面更接近的那个候选（歧义消解，不是猜）
            city_now = peer_local_time(city_tz, t)
            if city_now:
                hr = int(got["hour_raw"]) % 12
                cands = [hr, hr + 12]
                pick = min(cands, key=lambda c: min(abs(c - city_now["hour"]), 24 - abs(c - city_now["hour"])))
                if min(abs(pick - city_now["hour"]), 24 - abs(pick - city_now["hour"])) <= 1:
                    hint["hour"] = pick
                    hint["period"] = period_label(pick)
        if hint["hour"] is not None:
            hint["tz_guess"] = offset_from_statement(int(hint["hour"]), int(hint["minute"]), t)
        if cid:
            old = read_hint(inbox_store, cid, now=t)
            if old and float(old.get("ts") or 0) > t:
                return None
            _write_hint(inbox_store, cid, hint)
        logger.info("[peer-time] hint conv=%s kind=%s hour=%s period=%s tz_guess=%s ttl=12h evidence=%r",
                    cid or "-", hint["kind"], hint["hour"] if hint["hour"] is not None else "-",
                    hint["period"] or "-", hint["tz_guess"] or "-", hint["evidence"][:60])
        return hint
    except Exception:
        logger.debug("[peer-time] note_inbound failed", exc_info=True)
        return None


def _rows(inbox_store: Any, conversation_id: str, rows: Any = None) -> List[Dict[str, Any]]:
    if rows is not None:
        return [r for r in rows if isinstance(r, dict)]
    st = _store(inbox_store)
    cid = str(conversation_id or "").strip()
    if st is None or not cid or not hasattr(st, "list_recent_messages"):
        return []
    try:
        return [r for r in (st.list_recent_messages(cid, limit=_ROWS_LIMIT) or []) if isinstance(r, dict)]
    except Exception:
        return []


def _is_in(row: Dict[str, Any]) -> bool:
    d = str(row.get("direction") or row.get("dir") or "").strip().lower()
    if d:
        return d in ("in", "inbound")
    return str(row.get("role") or "") == "user"


def _row_ts(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_text(row: Dict[str, Any]) -> str:
    return str(row.get("text") or row.get("content") or "").strip()


def scan_inbound_for_statement(rows: Iterable[Dict[str, Any]], *, now: Optional[float] = None,
                               ttl_sec: float = HINT_TTL_SEC) -> Optional[Tuple[str, float]]:
    """最近 ``ttl_sec`` 内**最新**一条含时间叙述的客户入站 → ``(text, ts)``；无 → None。"""
    n = float(now if now is not None else time.time())
    for r in sorted((r for r in rows if isinstance(r, dict)), key=_row_ts, reverse=True):
        if not _is_in(r):
            continue
        ts = _row_ts(r)
        if ts <= 0 or n - ts > float(ttl_sec) or ts > n + 5:
            continue
        t = _row_text(r)
        if t and detect_time_statement(t) is not None:
            return t, ts
    return None


def resolve_hint(inbox_store: Any, conversation_id: str, *, now: Optional[float] = None, rows: Any = None,
                 city_tz: Optional[str] = None, write: bool = True) -> Optional[Dict[str, Any]]:
    """12h 内的 peer_time_hint：先读 KV；再扫最近入站（入站链没接 :func:`note_inbound` 时的无状态兜底），
    扫到更新的就（过门后）写回。→ hint | None。"""
    cid = str(conversation_id or "").strip()
    n = float(now if now is not None else time.time())
    h = read_hint(inbox_store, cid, now=n) if cid else None
    try:
        found = scan_inbound_for_statement(_rows(inbox_store, cid, rows), now=n)
        if found and (h is None or found[1] > float(h.get("ts") or 0)):
            if write:
                fresh = note_inbound(found[0], cid, inbox_store=inbox_store, ts=found[1], city_tz=city_tz)
            else:
                got = detect_time_statement(found[0])
                fresh = dict(got, ts=found[1], ttl_sec=HINT_TTL_SEC, evidence=got.get("sentence", ""), tz_guess="") if got else None
                if fresh and fresh.get("hour") is not None:
                    fresh["tz_guess"] = offset_from_statement(int(fresh["hour"]), int(fresh.get("minute") or 0), found[1])
            if fresh:
                h = fresh
    except Exception:
        logger.debug("[peer-time] resolve_hint scan failed", exc_info=True)
    return h


def last_time_question_ts(rows: Iterable[Dict[str, Any]], *, now: Optional[float] = None,
                          window_sec: float = ASK_WINDOW_SEC) -> float:
    """24h 内我方最近一次问对方几点 / 白天还是晚上的出站 ts；无 → 0。"""
    try:
        from src.inbox.repeat_question_guard import is_time_question, norm_lang, split_sentences
    except Exception:
        return 0.0
    n = float(now if now is not None else time.time())
    best = 0.0
    for r in rows or []:
        if not isinstance(r, dict) or _is_in(r):
            continue
        ts = _row_ts(r)
        if ts <= 0 or n - ts > float(window_sec) or ts > n + 5:
            continue
        t = _row_text(r)
        if not t:
            continue
        lg = norm_lang("", t)
        if any(is_time_question(s) for s in split_sentences(t, lg)):
            best = max(best, ts)
    return best


# ---------------------------------------------------------------------------
# B. 装配
# ---------------------------------------------------------------------------

def resolve_cfg(config: Any) -> Dict[str, Any]:
    node: Any = config if isinstance(config, dict) else {}
    for part in CFG_PATH.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def is_enabled(config: Any) -> bool:
    """显式 ``enabled`` 优先；缺席 → 陪伴域开、销售域关。"""
    cfg = resolve_cfg(config)
    if "enabled" in cfg and cfg.get("enabled") is not None:
        return bool(cfg.get("enabled"))
    try:
        from src.utils.business_domain import active_business_domain
        return active_business_domain(config if isinstance(config, dict) else None) == "companion"
    except Exception:
        return False


def _split_conv(conversation_id: str) -> Tuple[str, str, str]:
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def load_profile(conversation_id: str, goal_store: Any = None) -> Optional[Dict[str, Any]]:
    """会话 → 画像行（``{fields}``）。只用**已存在**的 GoalStore 单例（``peek_goal_store``），绝不新建。"""
    plat, _acct, ck = _split_conv(conversation_id)
    if not plat or not ck:
        return None
    st = goal_store
    if st is None:
        try:
            from src.companion.goals.store import peek_goal_store
            st = peek_goal_store()
        except Exception:
            st = None
    if st is None or not hasattr(st, "get_customer_profile"):
        return None
    try:
        return st.get_customer_profile(plat, ck) or None
    except Exception:
        return None


def peer_time_status(conversation_id: str, *, inbox_store: Any = None, goal_store: Any = None,
                     profile: Any = None, rows: Any = None, now: Optional[float] = None,
                     write: bool = True) -> Dict[str, Any]:
    """三源合一 → ``info``（交 ``time_context.build_peer_time_hint`` 格式化）::

        {known, source: hint|city|hint+city|"", tz, hh_mm, weekday, weekday_en, period, period_en,
         basis, basis_en, approx, conflict, hint_age_h, asked_ts, asked_hhmm, city_label}

    hint（12h 内客户原话）优先于城市推断；hint 有钟点 → 反推偏移，与城市时区相差 ≤1h 取城市精确钟，
    否则以客户原话为准（``conflict=True``）。都没有 → ``known=False`` + 24h 内是否已问过。"""
    cid = str(conversation_id or "").strip()
    n = float(now if now is not None else time.time())
    info: Dict[str, Any] = {
        "known": False, "source": "", "tz": "", "hh_mm": "", "weekday": "", "weekday_en": "", "period": "",
        "period_en": "", "basis": "", "basis_en": "", "approx": False, "conflict": False, "hint_age_h": None,
        "asked_ts": 0.0, "asked_hhmm": "", "city_label": "",
    }
    rows_l = _rows(inbox_store, cid, rows)
    prof = profile if profile is not None else load_profile(cid, goal_store)
    place = resolve_peer_place(prof, history=rows_l) if (prof or rows_l) else None
    city_tz, label = (place if place else (None, ""))
    info["city_label"] = label
    hint = resolve_hint(inbox_store, cid, now=n, rows=rows_l, city_tz=city_tz, write=write) if cid or rows_l else None

    def _said(h: Dict[str, Any]) -> Tuple[str, str]:
        hh = time.strftime("%H:%M", time.localtime(float(h.get("ts") or n)))
        ev = str(h.get("evidence") or h.get("match") or "").strip()[:40]
        return (f"对方 {hh} 说过「{ev}」", f'they said "{ev}" at {hh}')

    if hint:
        age_h = max(0.0, (n - float(hint.get("ts") or n)) / 3600.0)
        info["hint_age_h"] = round(age_h, 1)
        info["hint_ts"] = float(hint.get("ts") or 0)
        said_zh, said_en = _said(hint)
        if hint.get("hour") is not None:
            guess = str(hint.get("tz_guess") or offset_from_statement(int(hint["hour"]), int(hint.get("minute") or 0), float(hint["ts"])))
            tz_eff, src = guess, "hint"
            if city_tz:
                og, oc = _tz_offset_hours(guess, n), _tz_offset_hours(city_tz, n)
                if og is not None and oc is not None and abs(og - oc) <= 1.0:
                    tz_eff, src = city_tz, "hint+city"
                else:
                    info["conflict"] = True
            lt = peer_local_time(tz_eff, n)
            if lt:
                if src == "hint+city":
                    b_zh, b_en = f"，与画像城市「{label}」一致", f", consistent with their city ({label})"
                elif info["conflict"]:
                    b_zh, b_en = "（与画像城市不符，以对方原话为准）", " (differs from their profile city; their own words win)"
                else:
                    b_zh, b_en = "，据此推算", ", extrapolated"
                info.update({"known": True, "source": src, "tz": tz_eff, "hh_mm": lt["hh_mm"], "weekday": lt["weekday"],
                             "weekday_en": lt["weekday_en"], "period": lt["period"], "period_en": lt["period_en"],
                             "approx": src == "hint", "basis": said_zh + b_zh, "basis_en": said_en + b_en})
                return _finish(info, cid, rows_l, n)
        # 只有时段
        p = str(hint.get("period") or "")
        if city_tz:
            lt = peer_local_time(city_tz, n)
            if lt and (not p or lt["period"] == p or age_h >= 3.0):
                info.update({"known": True, "source": "hint+city", "tz": city_tz, "hh_mm": lt["hh_mm"], "weekday": lt["weekday"],
                             "weekday_en": lt["weekday_en"], "period": lt["period"], "period_en": lt["period_en"],
                             "basis": f"对方说过在{label}；{said_zh}", "basis_en": f"they live in {label}; {said_en}"})
                return _finish(info, cid, rows_l, n)
            info["conflict"] = True
        info.update({"known": True, "source": "hint", "period": p, "period_en": PERIOD_EN.get(p, ""), "approx": True,
                     "basis": said_zh + ("（与画像城市不符，以对方原话为准）" if info["conflict"] else "")
                              + (f"，已过 {age_h:.0f} 小时，时段可能已推移" if age_h >= 3.0 else ""),
                     "basis_en": said_en + (" (differs from their profile city; their own words win)" if info["conflict"] else "")
                                 + (f", {age_h:.0f}h ago so it may have moved on" if age_h >= 3.0 else "")})
        return _finish(info, cid, rows_l, n)
    if city_tz:
        lt = peer_local_time(city_tz, n)
        if lt:
            info.update({"known": True, "source": "city", "tz": city_tz, "hh_mm": lt["hh_mm"], "weekday": lt["weekday"],
                         "weekday_en": lt["weekday_en"], "period": lt["period"], "period_en": lt["period_en"],
                         "basis": f"对方说过在{label}", "basis_en": f"they said they're in {label}"})
            return _finish(info, cid, rows_l, n)
    return _finish(info, cid, rows_l, n)


def _finish(info: Dict[str, Any], cid: str, rows_l: List[Dict[str, Any]], n: float) -> Dict[str, Any]:
    if not info.get("known"):
        ts = last_time_question_ts(rows_l, now=n)
        info["asked_ts"] = ts
        info["asked_hhmm"] = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        logger.info("[peer-time] conv=%s known=0 asked=%s", cid or "-", info["asked_hhmm"] or "-")
    else:
        logger.info("[peer-time] conv=%s known=1 source=%s tz=%s local=%s period=%s%s", cid or "-", info["source"],
                    info["tz"] or "-", info["hh_mm"] or "-", info["period"] or "-",
                    " conflict=1" if info.get("conflict") else "")
    return info


def peer_time_known(conversation_id: str, *, inbox_store: Any = None, now: Optional[float] = None,
                    rows: Any = None, config: Any = None) -> Tuple[bool, str, float]:
    """出站守卫用：对方当地时间是否已知 → ``(known, source, hint_ts)``。只读（不写 KV）。
    功能关（``companion.peer_time.enabled`` / 业务域默认）→ ``(False, "", 0)``。绝不抛。"""
    try:
        if not is_enabled(config):
            return False, "", 0.0
        info = peer_time_status(conversation_id, inbox_store=inbox_store, rows=rows, now=now, write=False)
        ts = float(info.get("hint_ts") or 0) if str(info.get("source") or "").startswith("hint") else 0.0
        return bool(info.get("known")), str(info.get("source") or ""), ts
    except Exception:
        return False, "", 0.0


def build_peer_time_addendum(conversation_id: str, config: Any, *, lang: str = "zh", now: Optional[float] = None,
                             inbox_store: Any = None, goal_store: Any = None, profile: Any = None,
                             rows: Any = None) -> str:
    """生成前一段（``excuse_budget.build_time_schedule_addendum`` 尾部追加）。关 / 无会话 / 无 InboxStore
    （连「问过没」都判不了）→ ""。绝不抛。"""
    try:
        if not is_enabled(config):
            return ""
        cid = str(conversation_id or "").strip()
        if not cid:
            return ""
        st = _store(inbox_store)
        if rows is None and (st is None or not hasattr(st, "list_recent_messages")):
            return ""
        info = peer_time_status(cid, inbox_store=st, goal_store=goal_store, profile=profile, rows=rows, now=now)
        from src.inbox.time_context import build_peer_time_hint
        return build_peer_time_hint(info, lang=lang)
    except Exception:
        logger.debug("[peer-time] build_peer_time_addendum failed", exc_info=True)
        return ""


__all__ = [
    "CFG_PATH", "HINT_KEY_PREFIX", "HINT_TTL_SEC", "ASK_WINDOW_SEC", "PERIODS", "PERIOD_EN", "CITY_TZ", "COUNTRY_TZ",
    "period_label", "city_to_tz", "city_label", "resolve_peer_place", "resolve_peer_tz", "peer_local_time",
    "offset_from_statement", "detect_time_statement", "hint_key", "read_hint", "note_inbound",
    "scan_inbound_for_statement", "resolve_hint", "last_time_question_ts", "is_enabled", "load_profile",
    "peer_time_status", "peer_time_known", "build_peer_time_addendum",
]
