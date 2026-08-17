# Multi-language: en, zh (简体中文), tl (Tagalog). No hardcoded user-facing strings.
import re
from typing import Any

SUPPORTED_LOCALES = ("en", "zh", "tl")
DEFAULT_LOCALE = "en"

# Key -> locale -> template (use {score}, {risk}, {tips}, etc.)
TEXTS: dict[str, dict[str, str]] = {
    "welcome": {
        "en": (
            "👋 **TrustCheck** — Check credit & risk before you trade.\n\n"
            "• /check with ID, @username, or wallet — or **forward a message** from the person and I'll look them up automatically\n"
            "• Data: user reports + public info. For fraud prevention only.\n"
            "• We do not store your query history by default.\n\n"
            "Example: `/check 123456789` or forward their message here"
        ),
        "zh": (
            "👋 **信用查** — 交易前查信用与风险。\n\n"
            "• 使用 /check + 数字ID、@用户名 或钱包 — 也可**转发对方一条消息**自动查询\n"
            "• **对方隐藏了来源？** 用 /invite 获取链接发给 TA，TA 点开发消息后报告发给你\n"
            "• 数据来源：用户举报 + 公开信息，仅用于防诈。\n"
            "• 默认不存储您的查询记录。\n\n"
            "示例：`/check 123456789` 或转发对方消息到此处"
        ),
        "tl": (
            "👋 **TrustCheck** — Suriin ang credit at risk bago mag-trade.\n\n"
            "• /check sa ID, @username, o wallet — o **i-forward ang mensahe** nila dito at hahanapin ko sila\n"
            "• Data: user reports + public info. Para sa fraud prevention lang.\n"
            "• Hindi namin itinatago ang iyong query history by default.\n\n"
            "Halimbawa: `/check 123456789` o i-forward ang mensahe nila"
        ),
    },
    "btn_query": {"en": "🔍 Query now", "zh": "🔍 立即查询", "tl": "🔍 Mag-query na"},
    "btn_help": {"en": "❓ Help", "zh": "❓ 帮助", "tl": "❓ Tulong"},
    "check_usage": {
        "en": "Usage: /check <Telegram ID | @username | wallet address>\nExample: /check @durov",
        "zh": "用法：/check <Telegram ID | @用户名 | 钱包地址>\n示例：/check @用户名",
        "tl": "Gamit: /check <Telegram ID | @username | wallet address>\nHalimbawa: /check @durov",
    },
    "check_usage_simple": {
        "en": "Please enter the person to check:\n• Numeric ID (e.g. 123456789)\n• @username (e.g. @xxx)\n• Wallet address\nNo command needed — I'll recognize it and run the check.",
        "zh": "请直接输入要查询的对象：\n• 数字 ID（如 123456789）\n• @用户名（如 @xxx）\n• 钱包地址\n无需加任何命令，输入后我会自动识别并查询。",
        "tl": "Pakilagay ang taong iche-check:\n• Numeric ID (hal. 123456789)\n• @username (hal. @xxx)\n• Wallet address\nWalang kailangang command — makikilala ko at iche-check.",
    },
    "check_input_empty": {
        "en": "Please enter an ID, @username, or wallet address.",
        "zh": "请输入数字 ID、@用户名 或钱包地址。",
        "tl": "Pakilagay ng ID, @username, o wallet address.",
    },
    "check_cancelled": {
        "en": "Cancelled. Tap «Query now» again to enter an ID/username/wallet.",
        "zh": "已取消。请再次点击「立即查询」后输入要查的对象。",
        "tl": "Kinansela. I-tap muli ang «Query now» at ilagay ang ID/username/wallet.",
    },
    "check_loading": {"en": "Checking…", "zh": "查询中…", "tl": "Sinusuri…"},
    "check_error": {"en": "Request failed. Please try again later.", "zh": "请求失败，请稍后重试。", "tl": "Nabigo ang request. Pakisubukan mamaya."},
    "check_not_found": {
        "en": "No record found for this ID/address. **No record does not mean safe.**",
        "zh": "未找到该 ID/地址的记录。**无记录不代表安全。**",
        "tl": "Walang record para sa ID/address na ito. **Ang walang record ay hindi nangangahulugang safe.**",
    },
    "result_title": {"en": "📊 TrustCheck result", "zh": "📊 信用查结果", "tl": "📊 Resulta ng TrustCheck"},
    "risk_basis": {"en": "Risk is computed from the factors above (tips, reports, blacklist).", "zh": "风险依据：上述提示、举报与黑名单数据综合计算。", "tl": "Ang risk ay kinakalkula mula sa mga salik sa itaas (tips, reports, blacklist)."},
    "result_score": {"en": "Score: {score}/100", "zh": "信用分：{score}/100", "tl": "Score: {score}/100"},
    "result_risk": {"en": "Risk: {risk}", "zh": "风险：{risk}", "tl": "Risk: {risk}"},
    "result_blacklist": {"en": "⚠️ Listed on blacklist", "zh": "⚠️ 已列入黑名单", "tl": "⚠️ Nasa blacklist"},
    "result_tips": {"en": "Tips:\n{tips}", "zh": "提示：\n{tips}", "tl": "Tips:\n{tips}"},
    "result_disclaimer": {"en": "For reference only. Trade with caution.", "zh": "仅供参考，交易需谨慎。", "tl": "Para sa reference lang. Mag-ingat sa trade."},
    "report_footer_verify": {"en": "🔐 Verification code: `{hash}` — tap «Verify report» and enter this code.", "zh": "🔐 验真码：`{hash}` — 点击下方「验真报告」按钮，输入此码即可验真。", "tl": "🔐 Verification code: `{hash}` — i-tap ang «Verify report» at ilagay ang code."},
    "verify_valid": {"en": "✅ This report is **valid**. Generated at {created_at} UTC.", "zh": "✅ 该报告**真实有效**。生成于 {created_at} UTC。", "tl": "✅ **Valid** ang report. Na-generate noong {created_at} UTC."},
    "verify_invalid": {"en": "⚠️ This code was not found, or the report may have been altered.", "zh": "⚠️ 未找到该验真码，或报告可能已被篡改。", "tl": "⚠️ Hindi mahanap ang code, o maaaring nabago ang report."},
    "verify_usage": {"en": "Tap «Verify report» below, or enter the code (e.g. TC-78A2B9) here.", "zh": "请点击下方「验真报告」按钮，或在此处直接输入验真码（如 TC-78A2B9）。", "tl": "I-tap ang «Verify report» o ilagay ang code (hal. TC-78A2B9) dito."},
    "verify_enter_prompt": {"en": "Enter the verification code (e.g. TC-78A2B9), or paste the full report to check for tampering.", "zh": "请输入验真码（如 TC-78A2B9），或粘贴报告全文以校验是否被篡改。", "tl": "Ilagay ang code (hal. TC-78A2B9), o i-paste ang buong report para suriin kung nabago."},
    "verify_content_match_yes": {"en": "Content matches the archived report (not tampered).", "zh": "报告内容与存档一致，未被篡改。", "tl": "Ang content ay tumutugma sa naka-archive na report (hindi nabago)."},
    "verify_content_match_no": {"en": "⚠️ Content does not match the archive; the report may have been altered.", "zh": "⚠️ 内容与存档不一致，报告可能已被篡改。", "tl": "⚠️ Ang content ay hindi tumutugma sa archive; maaaring nabago ang report."},
    "btn_verify_report": {"en": "Verify report", "zh": "验真报告", "tl": "Verify report"},
    "source_official": {"en": "System", "zh": "官方", "tl": "System"},
    "source_crowd": {"en": "Community", "zh": "众包", "tl": "Community"},
    "source_network": {"en": "Monitor", "zh": "监控", "tl": "Monitor"},
    "source_on_chain": {"en": "Chain", "zh": "链上", "tl": "Chain"},
    "btn_privacy_settings": {"en": "🔒 Privacy settings", "zh": "🔒 隐私设置", "tl": "🔒 Privacy settings"},
    "btn_wallets": {"en": "💳 Payment addresses", "zh": "💳 收款地址", "tl": "💳 Payment addresses"},
    "wallets_title": {"en": "Your bound payment addresses", "zh": "你绑定的收款地址", "tl": "Your bound payment addresses"},
    "wallets_empty": {"en": "No addresses yet. Add one so others can see your flow (optional).", "zh": "暂无绑定。添加后可选「对查阅者展示流水」以增强信任。", "tl": "No addresses yet. Add one so others can see your flow (optional)."},
    "wallets_add": {"en": "➕ Add address", "zh": "➕ 添加地址", "tl": "➕ Add address"},
    "wallets_my_flow": {"en": "📜 My flow", "zh": "📜 我的流水", "tl": "📜 My flow"},
    "wallet_chain_trc20": {"en": "TRC20 (USDT)", "zh": "TRC20 (USDT)", "tl": "TRC20 (USDT)"},
    "wallet_chain_ton": {"en": "TON", "zh": "TON", "tl": "TON"},
    "wallet_show_flow_on": {"en": "Show flow ✓", "zh": "展示流水 ✓", "tl": "Show flow ✓"},
    "wallet_show_flow_off": {"en": "Hide flow", "zh": "不展示流水", "tl": "Hide flow"},
    "wallet_unbind": {"en": "Remove", "zh": "解绑", "tl": "Remove"},
    "wallet_add_choose_chain": {"en": "Choose chain:", "zh": "请选择链：", "tl": "Choose chain:"},
    "wallet_add_paste_address": {"en": "Paste the {chain} address (one line):", "zh": "请粘贴 {chain} 地址（一行）：", "tl": "Paste the {chain} address (one line):"},
    "wallet_add_ok": {"en": "Added. You can toggle «Show flow» so others see your transactions.", "zh": "已添加。可开关「展示流水」让查阅者看到该地址近期交易。", "tl": "Added. You can toggle «Show flow» so others see your transactions."},
    "wallet_add_invalid": {"en": "Invalid address format. Check and try again.", "zh": "地址格式不正确，请检查后重试。", "tl": "Invalid address format. Check and try again."},
    "wallet_add_limit": {"en": "Max addresses reached. Remove one first.", "zh": "已达上限，请先解绑一个。", "tl": "Max addresses reached. Remove one first."},
    "wallet_add_duplicate": {"en": "This address is already bound.", "zh": "该地址已绑定。", "tl": "This address is already bound."},
    "flow_title_mine": {"en": "📜 My flow (recent)", "zh": "📜 我的流水（近期）", "tl": "📜 My flow (recent)"},
    "flow_title_other": {"en": "📜 Their flow (recent)", "zh": "📜 对方流水（近期）", "tl": "📜 Their flow (recent)"},
    "flow_empty": {"en": "No transactions in this period.", "zh": "该时段暂无交易记录。", "tl": "No transactions in this period."},
    "flow_error_no_wallets": {"en": "They have not opened flow for viewers.", "zh": "对方未开放流水。", "tl": "They have not opened flow for viewers."},
    "flow_other_from_db_note": {"en": "(from their local snapshot)", "zh": "（来自对方本地快照）", "tl": "(from their local snapshot)"},
    "flow_line": {"en": "{dir} {amount} {currency} · {counterparty} · {chain}", "zh": "{dir} {amount} {currency} · {counterparty} · {chain}", "tl": "{dir} {amount} {currency} · {counterparty} · {chain}"},
    "flow_in": {"en": "In", "zh": "入", "tl": "In"},
    "flow_out": {"en": "Out", "zh": "出", "tl": "Out"},
    "view_flow_btn": {"en": "📜 View their flow", "zh": "📜 查看对方流水", "tl": "📜 View their flow"},
    "flow_disclaimer": {"en": "Chain data, for reference only. Verify before trading.", "zh": "链上数据仅供参考，交易前请自行核实。", "tl": "Chain data, for reference only. Verify before trading."},
    "wallet_verified": {"en": "Verified", "zh": "已验证", "tl": "Verified"},
    "wallet_unverified": {"en": "Unverified", "zh": "未验证", "tl": "Unverified"},
    "flow_rate_limit": {"en": "Too many requests. Please try again in a minute.", "zh": "请求过于频繁，请稍后再试。", "tl": "Too many requests. Try again in a minute."},
    "rate_limit_check": {"en": "Query limit reached. Please try again in a minute.", "zh": "查询次数已达上限，请稍后再试。", "tl": "Naabot na ang limit ng query. Subukan muli sa isang minuto."},
    "rate_limit_report": {"en": "Report limit reached. Please try again in a minute.", "zh": "举报次数已达上限，请稍后再试。", "tl": "Naabot na ang limit ng report. Subukan muli sa isang minuto."},
    "wallets_sync_flow": {"en": "📥 Sync to local", "zh": "📥 同步到本地", "tl": "📥 Sync to local"},
    "flow_synced": {"en": "Synced {n} new record(s) to local. You can view them under «From local».", "zh": "已同步 {n} 条新记录到本地，可在「从本地查看」中查看。", "tl": "Synced {n} new record(s). View under «From local»."},
    "flow_sync_none": {"en": "No new records (already up to date).", "zh": "暂无新记录（已是最新）。", "tl": "No new records (already up to date)."},
    "flow_stats_30d_line": {
        "en": "Last {days} days: in {in_count} (≈{in_amount}), out {out_count} (≈{out_amount}).",
        "zh": "近 {days} 天：流入 {in_count} 笔（≈{in_amount}），流出 {out_count} 笔（≈{out_amount}）。",
        "tl": "Huling {days} araw: pasok {in_count} (≈{in_amount}), labas {out_count} (≈{out_amount}).",
    },
    "flow_sync_rate_limited": {"en": "Sync is limited to once every few minutes. Please try again in {minutes} min.", "zh": "同步操作每几分钟仅可进行一次，请 {minutes} 分钟后再试。", "tl": "Sync limited. Try again in {minutes} min."},
    "flow_from_db": {"en": "📂 From local", "zh": "📂 从本地查看", "tl": "📂 From local"},
    "flow_from_db_empty": {"en": "No local data. Tap «Sync to local» first.", "zh": "暂无本地数据，请先点击「同步到本地」。", "tl": "No local data. Tap «Sync to local» first."},
    "flow_from_db_page": {"en": "(local snapshot, from #{offset})", "zh": "（本地快照，第 {offset}+ 条起）", "tl": "(local snapshot, from #{offset})"},
    "flow_from_db_next": {"en": "Next page", "zh": "下一页", "tl": "Next page"},
    "flow_from_db_prev": {"en": "Prev page", "zh": "上一页", "tl": "Prev page"},
    "privacy_title": {"en": "Privacy: what others see when they check you", "zh": "隐私：他人查你时可见的项", "tl": "Privacy: ano ang makikita ng iba kapag iche-check ka nila"},
    "privacy_intro": {"en": "Toggle what to show in your report. OFF = hidden from others.", "zh": "开关控制报告中对他人的展示。关闭 = 他人不可见。", "tl": "I-toggle kung ano ang ipapakita sa iyong report. OFF = nakatago sa iba."},
    "privacy_option_register_year": {"en": "First recorded (registration)", "zh": "首次记录（注册）", "tl": "Unang na-record (registration)"},
    "privacy_option_premium": {"en": "Telegram Premium", "zh": "Telegram 会员", "tl": "Telegram Premium"},
    "privacy_option_tx_30d": {"en": "Transaction stats (success count / rate)", "zh": "交易笔数/成功率", "tl": "Transaction stats (tagumpay / rate)"},
    "privacy_value_on": {"en": "✓ Shown", "zh": "✓ 展示", "tl": "✓ Ipinakita"},
    "privacy_value_off": {"en": "Hidden", "zh": "隐藏", "tl": "Nakatago"},
    "privacy_updated": {"en": "Settings updated.", "zh": "设置已更新。", "tl": "Na-update ang settings."},
    "detail_section": {"en": "📋 Details", "zh": "📋 详细信息", "tl": "📋 Detalye"},
    "detail_first_seen": {"en": "First recorded", "zh": "首次记录", "tl": "Unang na-record"},
    "detail_first_seen_note": {"en": "(in our system; Telegram does not provide registration date)", "zh": "（本系统首次记录；Telegram 不提供注册时间）", "tl": "(sa aming system; hindi ibinibigay ng Telegram ang registration date)"},
    "detail_report_count": {"en": "Reports against", "zh": "被举报次数", "tl": "Mga report laban"},
    "detail_blacklist": {"en": "On blacklist", "zh": "黑名单", "tl": "Sa blacklist"},
    "detail_blacklist_yes": {"en": "Yes", "zh": "是", "tl": "Oo"},
    "detail_blacklist_no": {"en": "No", "zh": "否", "tl": "Hindi"},
    "detail_premium": {"en": "Telegram Premium", "zh": "Telegram 会员", "tl": "Telegram Premium"},
    "detail_privacy_note": {
        "en": "Telegram does not give bots: registration date, last seen, or group/friend count. Above = our system only.",
        "zh": "Telegram 不向机器人提供注册时间、最后在线、群/好友数；以上仅为本系统数据。",
        "tl": "Hindi ibinibigay ng Telegram sa bots ang registration date, last seen, o bilang ng group/kaibigan. Data above = aming system lang.",
    },
    "detail_verdict": {"en": "Verdict", "zh": "综合建议", "tl": "Verdict"},
    "detail_tags": {"en": "Tags", "zh": "标签", "tl": "Mga tag"},
    "section_tags": {"en": "🏷 Tags", "zh": "🏷 标签", "tl": "🏷 Mga tag"},
    "section_risk_dimensions": {"en": "📊 Risk dimensions", "zh": "📊 风险维度", "tl": "📊 Risk dimensions"},
    "dim_identity": {"en": "Identity stability", "zh": "身份稳定性", "tl": "Identity stability"},
    "dim_funds": {"en": "Funds / collateral", "zh": "资金实力", "tl": "Funds / collateral"},
    "dim_community": {"en": "Community reputation", "zh": "社区评价", "tl": "Community reputation"},
    "dim_association": {"en": "Association risk", "zh": "关联风险", "tl": "Association risk"},
    "dim_level_high": {"en": "High", "zh": "高", "tl": "High"},
    "dim_level_medium": {"en": "Medium", "zh": "中", "tl": "Medium"},
    "dim_level_low": {"en": "Low", "zh": "低", "tl": "Low"},
    "dim_level_unverified": {"en": "Unverified", "zh": "未验证", "tl": "Unverified"},
    "dim_level_unknown": {"en": "Unknown", "zh": "未知", "tl": "Unknown"},
    "tag_same_wallet_risk": {"en": "Same-wallet risk", "zh": "换号嫌疑", "tl": "Same-wallet risk"},
    "section_verdict": {"en": "📋 Verdict", "zh": "📋 综合建议", "tl": "📋 Verdict"},
    "section_details": {"en": "📋 Details", "zh": "📋 详细信息", "tl": "📋 Detalye"},
    "detail_query_count": {"en": "Times queried", "zh": "被查询次数", "tl": "Beses na na-query"},
    "detail_updated_at": {"en": "Data updated", "zh": "数据更新于", "tl": "Na-update ang data"},
    "detail_last_seen": {"en": "Last activity (in our system)", "zh": "最近活动（本系统）", "tl": "Huling aktibidad (sa aming system)"},
    "data_disclaimer_header": {"en": "📌 Data notice", "zh": "📌 数据说明", "tl": "📌 Paunawa sa data"},
    "detail_reports_breakdown": {"en": "Reports: {total} total (pending {pending}, verified {verified})", "zh": "举报：共 {total} 次（待核实 {pending}，已核实 {verified}）", "tl": "Reports: {total} total (pending {pending}, verified {verified})"},
    "verdict_extreme": {"en": "Very high risk; strongly recommend avoiding any transaction.", "zh": "风险极高，强烈建议避免交易。", "tl": "Napakataas na risk; inirerekomenda na iwasan ang anumang transaksyon."},
    "verdict_high": {"en": "High risk; recommend caution and extra verification.", "zh": "风险较高，建议谨慎并做好核实。", "tl": "Mataas na risk; mag-ingat at mag-double-check."},
    "verdict_medium": {"en": "Some risk; verify before deciding.", "zh": "存在一定风险，建议核实后决策。", "tl": "May risk; i-verify muna bago mag-desisyon."},
    "verdict_low": {"en": "No major risk in our data; use your judgment.", "zh": "当前数据暂无重大风险，请自行判断。", "tl": "Walang malaking risk sa aming data; gamitin ang iyong judgment."},
    "next_step_extreme": {"en": "Next step: Avoid transaction, or ask them to show their report first.", "zh": "下一步：建议暂不交易；若必须接触，可要求对方出示报告后再决定。", "tl": "Susunod: Iwasan ang transaksyon, o hilingin muna ang kanilang report."},
    "next_step_high": {"en": "Next step: Try small amount first, or ask them to show their report before large deal.", "zh": "下一步：建议先小额试单，或要求对方出示报告后再大额交易。", "tl": "Susunod: Subukan muna ang maliit na halaga, o hilingin ang report bago malaking deal."},
    "next_step_medium": {"en": "Next step: Verify before deciding; you may ask them to show their report.", "zh": "下一步：核实后再决策；可要求对方出示报告。", "tl": "Susunod: I-verify bago mag-desisyon; maaari mong hilingin ang kanilang report."},
    "next_step_low": {"en": "Next step: Use your judgment; you may still ask for their report for transparency.", "zh": "下一步：请自行判断；为透明起见仍可要求对方出示报告。", "tl": "Susunod: Gamitin ang iyong judgment; maaari pa ring humiling ng report para sa transparency."},
    "section_linked_wallets": {"en": "🔗 Linked wallets", "zh": "🔗 关联钱包", "tl": "🔗 Mga naka-link na wallet"},
    "section_wallet_other_accounts": {"en": "This wallet is also linked to these accounts (same wallet, different TG; be aware).", "zh": "该钱包还关联以下账号（换号不换地址，请留意）。", "tl": "Ang wallet na ito ay naka-link din sa mga account na ito (iisang wallet, ibang TG; maging aware)."},
    "section_who_queried_me": {"en": "👁 Who queried me", "zh": "👁 谁查过我", "tl": "👁 Sino ang nag-query sa akin"},
    "who_queried_me_7d_count": {"en": "Queries in last 7 days: {count}", "zh": "最近 7 天被查询次数：{count}", "tl": "Mga query sa huling 7 araw: {count}"},
    "who_queried_me_last_time": {"en": "Last queried at: {time}", "zh": "最近一次被查时间：{time}", "tl": "Huling na-query: {time}"},
    "section_timeline": {"en": "📅 Recent activity (this system)", "zh": "📅 最近活动（本系统）", "tl": "📅 Kamakailang aktibidad (sistema)"},
    "timeline_queried": {"en": "Queried {n} time(s)", "zh": "被查 {n} 次", "tl": "Na-query {n} beses"},
    "timeline_invite_clicked": {"en": "Invite opened {n}", "zh": "邀请打开 {n} 次", "tl": "Invite na-open {n}"},
    "timeline_share_viewed": {"en": "Report share viewed {n}", "zh": "报告分享被查看 {n} 次", "tl": "Report share na-view {n}"},
    "btn_show_my_report": {"en": "Show my report (share link)", "zh": "出示我的报告", "tl": "Ipakita ang aking report (share link)"},
    "share_link_instructions": {"en": "Share this link with the other party. They open it to see your latest report (one-time view, expires in {hours}h).", "zh": "将链接发给对方，对方点开即可查看你的最新报告（一次性查看，{hours} 小时内有效）。", "tl": "I-share ang link sa kabila. Bubuksan nila para makita ang iyong pinakabagong report (one-time view, mag-e-expire sa {hours}h)."},
    "share_link_label": {"en": "Your report share link", "zh": "我的报告分享链接", "tl": "Iyong report share link"},
    "share_expired": {"en": "This share link is invalid, expired, or already used.", "zh": "该链接已失效、过期或已被使用。", "tl": "Ang share link na ito ay invalid, expired, o nagamit na."},
    "share_loading": {"en": "Generating your share link…", "zh": "正在生成分享链接…", "tl": "Gumagawa ng iyong share link…"},
    "btn_ask_show_report": {"en": "Ask them to show report", "zh": "请对方出示报告", "tl": "Hilingin ang kanilang report"},
    "btn_forward_report": {"en": "Forward report", "zh": "转发报告", "tl": "I-forward ang report"},
    "btn_resend_report": {"en": "Resend report to me", "zh": "再次发送报告给我", "tl": "I-resend ang report sa akin"},
    "msg_resend_report": {"en": "Report sent. You can save it or forward it to other chats.", "zh": "报告已发送。您可以保存或转发到其他聊天。", "tl": "Na-send na ang report. Maaari mong i-save o i-forward sa ibang chat."},
    "msg_ask_show_report": {"en": "To ask the other party to show their report: they can send /mycredit in this bot to see their own report, or use «My credit» and forward the result to you. You can also send them an invite link so they open it and you receive their report.", "zh": "如何让对方出示报告：对方可在本机器人发送 /mycredit 查看自己的报告，或点击「我的信用」后将结果转发给你。你也可以发送邀请链接，对方点开后你会收到其报告。", "tl": "Para hilingin ang report ng kabila: maaari silang mag-send ng /mycredit sa bot para makita ang sariling report, o gamitin ang «My credit» at i-forward sa iyo. Maaari mo ring ipadala ang invite link para buksan nila at matanggap mo ang report."},
    "tag_new_account": {"en": "New account", "zh": "新账号", "tl": "Bagong account"},
    "tag_has_reports": {"en": "Has reports", "zh": "有举报", "tl": "May reports"},
    "tag_blacklist": {"en": "Blacklisted", "zh": "黑名单", "tl": "Naka-blacklist"},
    "tag_premium": {"en": "Premium", "zh": "会员", "tl": "Premium"},
    "tag_merchant": {"en": "Merchant", "zh": "商户", "tl": "Merchant"},
    "btn_report": {"en": "📌 Report / Incorrect?", "zh": "📌 举报 / 纠错?", "tl": "📌 I-report / Mali?"},
    "btn_my_credit": {"en": "My credit", "zh": "我的信用", "tl": "Aking credit"},
    "btn_recent_queries": {"en": "Recent queries", "zh": "最近查询", "tl": "Mga kamakailang query"},
    "btn_my_appeals": {"en": "My appeals", "zh": "我的申诉", "tl": "Aking mga appeal"},
    "recent_queries_title": {"en": "📋 Recent queries (tap to re-check)", "zh": "📋 最近查询（点击可再查）", "tl": "📋 Mga kamakailang query (i-tap para i-check muli)"},
    "recent_queries_empty": {"en": "No recent queries yet. Use «Query now» to check someone.", "zh": "暂无最近查询记录。请使用「立即查询」查人。", "tl": "Walang kamakailang query. Gamitin ang «Query now» para mag-check."},
    "recent_queries_item": {"en": "ID {tid} — {time}", "zh": "ID {tid} — {time}", "tl": "ID {tid} — {time}"},
    "btn_recheck": {"en": "Re-check", "zh": "再查", "tl": "I-check muli"},
    "feedback_title": {"en": "📋 My appeals (corrections / false reports)", "zh": "📋 我的申诉（纠错/误报反馈）", "tl": "📋 Aking mga appeal (mga koreksyon / maling report)"},
    "feedback_empty": {"en": "You have not submitted any appeals yet. Use «Report / Incorrect?» on a result to submit.", "zh": "您暂未提交过申诉。可在查询结果上点「举报/纠错」提交。", "tl": "Wala ka pang na-submit na appeal. Gamitin ang «Report / Incorrect?» sa result para mag-submit."},
    "feedback_item": {"en": "ID {tid}: {reason} — {status} ({date})", "zh": "对象 ID {tid}：{reason} — {status}（{date}）", "tl": "ID {tid}: {reason} — {status} ({date})"},
    "feedback_status_pending": {"en": "Pending", "zh": "待处理", "tl": "Pending"},
    "feedback_status_processed": {"en": "Processed", "zh": "已处理", "tl": "Na-process"},
    "feedback_status_rejected": {"en": "Rejected", "zh": "已驳回", "tl": "Tinanggihan"},
    "btn_alert_subscribe": {"en": "Notify me when risk changes", "zh": "有变化时提醒我", "tl": "I-notify ako kapag may pagbabago"},
    "btn_alert_unsubscribe": {"en": "Unsubscribe", "zh": "取消提醒", "tl": "I-unsubscribe"},
    "btn_my_alert_subscriptions": {"en": "My alert subscriptions", "zh": "我的订阅", "tl": "Aking mga subscription"},
    "msg_alert_subscribed": {"en": "Subscribed. You will be notified when this user has new reports or is blacklisted.", "zh": "已订阅。该用户有新举报或拉黑时会通知您。", "tl": "Naka-subscribe na. Ma-no-notify ka kapag may bagong report o na-blacklist ang user na ito."},
    "msg_alert_already": {"en": "You are already subscribed to this user.", "zh": "您已订阅过该用户。", "tl": "Naka-subscribe ka na sa user na ito."},
    "msg_alert_limit": {"en": "Subscription limit reached (max 20). Unsubscribe someone in «My alert subscriptions» first.", "zh": "订阅数已达上限（最多20个）。请先在「我的订阅」中取消部分后再试。", "tl": "Naabot na ang limit (max 20). Mag-unsubscribe muna sa «My alert subscriptions»."},
    "msg_alert_unsubscribed": {"en": "Unsubscribed.", "zh": "已取消订阅。", "tl": "Na-unsubscribe na."},
    "alert_my_subscriptions_title": {"en": "📋 My alert subscriptions (notify when they have new reports or blacklist)", "zh": "📋 我的订阅（有新举报或拉黑时通知我）", "tl": "📋 Aking mga subscription (notify kapag may bagong report o blacklist)"},
    "alert_my_subscriptions_empty": {"en": "No subscriptions yet. Use «Notify me when risk changes» on a check result to subscribe.", "zh": "暂无订阅。在查询结果上点「有变化时提醒我」即可订阅。", "tl": "Walang subscription. Gamitin ang «Notify me when risk changes» sa check result para mag-subscribe."},
    "alert_my_subscriptions_empty_invite_hint": {"en": "The same button appears under reports you receive via invite links.", "zh": "通过邀请链接收到的报告下方也有该按钮。", "tl": "Ang parehong button ay lalabas sa report na natanggap mo sa pamamagitan ng invite link."},
    "result_alert_tip": {"en": "Subscribe below to get notified when this user has new reports or is blacklisted.", "zh": "订阅后该用户有新举报或拉黑会通知您。", "tl": "Mag-subscribe sa ibaba para ma-notify kapag may bagong report o na-blacklist ang user na ito."},
    "alert_my_subscriptions_item": {"en": "ID {tid} — {type_label} subscribed {date}", "zh": "ID {tid} — {type_label} 订阅于 {date}", "tl": "ID {tid} — {type_label} naka-subscribe {date}"},
    "alert_push_new_report": {"en": "⚠️ Alert: User ID {tid} you follow has received a new report. Consider checking their report again.", "zh": "⚠️ 提醒：您关注的用户 ID {tid} 收到新举报，建议重新查询其报告。", "tl": "⚠️ Alert: Ang user ID {tid} na fina-follow mo ay may bagong report. Isaalang-alang ang pag-check muli ng report."},
    "alert_push_blacklist_added": {"en": "⚠️ Alert: User ID {tid} you follow has been added to the blacklist. Trade with caution.", "zh": "⚠️ 提醒：您关注的用户 ID {tid} 已被加入黑名单，请谨慎交易。", "tl": "⚠️ Alert: Ang user ID {tid} na fina-follow mo ay na-add na sa blacklist. Mag-ingat sa trade."},
    "alert_push_online": {"en": "🟢 User ID {tid} you follow is now online.", "zh": "🟢 您关注的用户 ID {tid} 当前在线。", "tl": "🟢 Ang user ID {tid} na fina-follow mo ay online na ngayon."},
    "alert_push_report_spike": {"en": "📈 Report spike: User ID {tid} you follow received a new report. Consider re-checking.", "zh": "📈 举报动态：您关注的用户 ID {tid} 收到新举报，建议重新查询。", "tl": "📈 Report spike: User ID {tid} na fina-follow mo ay may bagong report. Isaalang-alang ang muling pag-check."},
    "alert_push_wallet_change": {"en": "🔗 Wallet change: User ID {tid} you follow has a new wallet link. Consider re-checking.", "zh": "🔗 钱包变动：您关注的用户 ID {tid} 关联了新钱包，建议重新查询。", "tl": "🔗 Wallet change: User ID {tid} na fina-follow mo ay may bagong wallet link. Isaalang-alang ang muling pag-check."},
    "notify_checked_push": {"en": "📋 Someone checked your credit. Use /mycredit to see your report.", "zh": "📋 有人查了你的信用。使用「我的信用」查看报告。", "tl": "📋 May nag-check ng iyong credit. Gamitin ang /mycredit para makita ang report."},
    "notify_setting_on": {"en": "Notify when checked: ON", "zh": "被查通知：开", "tl": "Notify when checked: ON"},
    "notify_setting_off": {"en": "Notify when checked: OFF", "zh": "被查通知：关", "tl": "Notify when checked: OFF"},
    "notify_btn_turn_off": {"en": "Turn off", "zh": "关闭", "tl": "Turn off"},
    "notify_btn_turn_on": {"en": "Turn on", "zh": "开启", "tl": "Turn on"},
    "notify_turned_on": {"en": "You will be notified when someone checks your credit.", "zh": "已开启被查通知。", "tl": "You will be notified when someone checks your credit."},
    "notify_turned_off": {"en": "You will not be notified when someone checks your credit.", "zh": "已关闭被查通知。", "tl": "You will not be notified when someone checks your credit."},
    "sharecard_title": {"en": "📊 My credit card", "zh": "📊 我的信用卡片", "tl": "📊 My credit card"},
    "sharecard_role_merchant": {"en": "Merchant", "zh": "商户", "tl": "Merchant"},
    "sharecard_role_user": {"en": "User", "zh": "普通用户", "tl": "User"},
    "sharecard_updated": {"en": "Updated: {at}", "zh": "数据更新于 {at}", "tl": "Updated: {at}"},
    "sharecard_line_risk": {"en": "Risk: {risk}", "zh": "风险：{risk}", "tl": "Risk: {risk}"},
    "sharecard_risk_low": {"en": "🟢 Low", "zh": "🟢 低", "tl": "🟢 Low"},
    "sharecard_risk_medium": {"en": "🟡 Medium", "zh": "🟡 中", "tl": "🟡 Medium"},
    "sharecard_risk_high": {"en": "🟠 High", "zh": "🟠 高", "tl": "🟠 High"},
    "sharecard_risk_extreme": {"en": "🔴 Very high", "zh": "🔴 极高", "tl": "🔴 Very high"},
    "sharecard_line_score": {"en": "Score: {score}/100", "zh": "信用分：{score}/100", "tl": "Score: {score}/100"},
    "sharecard_score_hint_high": {"en": "(80–100: generally stable)", "zh": "（80–100：整体较稳）", "tl": "(80–100: generally stable)"},
    "sharecard_score_hint_mid": {"en": "(56–75: some risk)", "zh": "（56–75：有一定风险）", "tl": "(56–75: some risk)"},
    "sharecard_score_hint_low": {"en": "(0–55: higher risk)", "zh": "（0–55：风险较高）", "tl": "(0–55: higher risk)"},
    "sharecard_line_age": {"en": "First recorded: {date}", "zh": "官方 首次记录：{date}", "tl": "First recorded: {date}"},
    "sharecard_line_tx": {"en": "Confirmed trades: {count} ({rate}% success)", "zh": "交易确认：{count} 笔（成功率 {rate}%）", "tl": "Confirmed trades: {count} ({rate}% success)"},
    "sharecard_line_tx_none": {"en": "No trade records yet", "zh": "暂无交易记录", "tl": "No trade records yet"},
    "sharecard_line_crowd": {"en": "Reports: {total} (verified {verified}) · Blacklist: {bl}", "zh": "众包 举报：共 {total} 次（已核实 {verified}）｜黑名单：{bl}", "tl": "Reports: {total} (verified {verified}) · Blacklist: {bl}"},
    "sharecard_tags_intro": {"en": "Tags: ", "zh": "🏷 ", "tl": "Tags: "},
    "sharecard_tags_none": {"en": "No tags", "zh": "暂无标签", "tl": "No tags"},
    "sharecard_disclaimer": {"en": "For reference only. Verify and keep proof before trading.", "zh": "以上仅供参考，交易前请多核实、留凭证。", "tl": "For reference only. Verify and keep proof before trading."},
    "sharecard_verify_hint": {"en": "Others can tap «Verify report» and enter the code on the report to check the full report.", "zh": "他人可点击「验真报告」并输入报告底部验真码，核实完整报告。", "tl": "Others can tap «Verify report» and enter the code to check the full report."},
    "btn_sharecard": {"en": "Share card", "zh": "分享卡片", "tl": "Share card"},
    "scan_group_ok": {"en": "Group scan recorded. (Once per group per day.)", "zh": "群扫描已记录。（每群每日限 1 次。）", "tl": "Group scan recorded. (Once per group per day.)"},
    "scan_group_already": {"en": "This group was already scanned today. Try again tomorrow.", "zh": "本群今日已扫描过，请明日再试。", "tl": "This group was already scanned today. Try again tomorrow."},
    "scan_group_private": {"en": "Use /scan_group in a group (as admin).", "zh": "请在群组内使用 /scan_group（需为管理员）。", "tl": "Use /scan_group in a group (as admin)."},
    "scan_group_not_admin": {"en": "Only group admins can run /scan_group.", "zh": "仅群管理员可使用 /scan_group。", "tl": "Only group admins can run /scan_group."},
    "alert_push_offline": {"en": "🔴 User ID {tid} you follow is now offline.", "zh": "🔴 您关注的用户 ID {tid} 已下线。", "tl": "🔴 Ang user ID {tid} na fina-follow mo ay offline na."},
    "btn_subscribe_online_offline": {"en": "Subscribe: online/offline alerts", "zh": "订阅上下线提醒", "tl": "Subscribe: online/offline alerts"},
    "btn_report_online": {"en": "Report: TA online", "zh": "上报：TA 在线", "tl": "Report: TA online"},
    "btn_report_offline": {"en": "Report: TA offline", "zh": "上报：TA 离线", "tl": "Report: TA offline"},
    "msg_alert_subscribed_online": {"en": "Subscribed to online/offline alerts. You will be notified when this user goes online or offline (from reporter updates).", "zh": "已订阅上下线提醒。该用户上线或下线时会通知您（数据来自他人上报）。", "tl": "Naka-subscribe na sa online/offline alerts. Ma-no-notify ka kapag ang user na ito ay online o offline."},
    "msg_alert_subscribed_report_spike": {"en": "Subscribed to report alerts. You will be notified when this user receives a new report.", "zh": "已订阅举报动态。该用户收到新举报时会通知您。", "tl": "Naka-subscribe na sa report alerts. Ma-no-notify ka kapag may bagong report ang user na ito."},
    "msg_alert_subscribed_wallet_change": {"en": "Subscribed to wallet change. You will be notified when this user has a new wallet link.", "zh": "已订阅钱包变动。该用户关联新钱包时会通知您。", "tl": "Naka-subscribe na sa wallet change. Ma-no-notify ka kapag may bagong wallet link ang user na ito."},
    "alert_type_report_spike": {"en": "Report alerts", "zh": "举报动态", "tl": "Report alerts"},
    "alert_type_wallet_change": {"en": "Wallet change", "zh": "钱包变动", "tl": "Wallet change"},
    "btn_subscribe_report_spike": {"en": "Report alerts", "zh": "举报动态", "tl": "Report alerts"},
    "btn_subscribe_wallet_change": {"en": "Wallet change", "zh": "钱包变动", "tl": "Wallet change"},
    "alert_type_risk": {"en": "Risk alerts", "zh": "风险提醒", "tl": "Risk alerts"},
    "alert_type_online": {"en": "Online/offline", "zh": "上下线", "tl": "Online/offline"},
    "msg_status_reported": {"en": "Status reported. Subscribers will be notified.", "zh": "已上报。订阅者将收到通知。", "tl": "Na-report na. Ma-no-notify ang mga subscriber."},
    "mycredit_loading": {"en": "Loading your credit…", "zh": "加载中…", "tl": "Kinakarga ang iyong credit…"},
    "mycredit_error": {"en": "Could not load. Try again.", "zh": "加载失败，请重试。", "tl": "Hindi ma-load. Subukan muli."},
    "help_title": {"en": "📌 Help & usage", "zh": "📌 帮助与说明", "tl": "📌 Tulong at gamit"},
    "menu_hint": {"en": "👇 Use the menu below to continue", "zh": "👇 使用下方菜单继续", "tl": "👇 Gamitin ang menu sa ibaba"},
    "help_section_query_title": {"en": "🔍 How to query", "zh": "🔍 查询方式", "tl": "🔍 Paano mag-query"},
    "help_section_query": {
        "en": "/check <ID|@user|wallet> — Check someone's risk & score.\nOr **forward a message** from that user here — I'll look them up automatically.",
        "zh": "/check <数字ID|@用户|钱包> — 查询对方风险与信用分。也可**转发对方一条消息**到本机器人，自动查询该用户。",
        "tl": "/check <ID|@user|wallet> — Suriin ang risk at score. O **i-forward ang mensahe** nila dito — hahanapin ko sila.",
    },
    "help_section_invite_title": {"en": "🔗 Invite link", "zh": "🔗 邀请链接", "tl": "🔗 Invite link"},
    "help_section_invite": {
        "en": "When they hide their info: use /invite or «Get invite link» — send them the link; when they open it and message, you receive their report.",
        "zh": "对方隐藏来源时：用 /invite 或「获取邀请链接」— 把链接发给对方，对方点开发消息后，报告会发给你。",
        "tl": "Kapag nakatago ang info: gamitin ang /invite — ipadala ang link; pag binuksan nila at nag-message, matatanggap mo ang report.",
    },
    "help_section_mycredit_title": {"en": "📋 My credit & share", "zh": "📋 我的信用与出示", "tl": "📋 Aking credit at share"},
    "help_section_mycredit": {
        "en": "/mycredit or «My credit» — See your own report, who queried you recently, and «Show my report» to get a one-time link to send to others.",
        "zh": "/mycredit 或「我的信用」— 查看自己的报告、最近谁查过我，以及「出示我的报告」获取一次性链接发给对方。",
        "tl": "/mycredit o «My credit» — Makita ang iyong report, sino ang nag-query sa iyo, at «Show my report» para sa one-time link.",
    },
    "help_section_dont_trust_me_title": {"en": "🤝 They don't trust me?", "zh": "🤝 对方不信我怎么办？", "tl": "🤝 Hindi ako pinagkakatiwalaan?"},
    "help_section_dont_trust_me": {
        "en": "Tap «My credit» then «Show my report» — send the one-time link to them; they open it to see your report. Or use «Get invite link» and send them the link; when they open and message, you get their report.",
        "zh": "点「我的信用」后点「出示我的报告」— 把一次性链接发给对方，对方点开即可看到你的报告。也可用「获取邀请链接」把链接发给对方，对方点开发消息后你会收到其报告。",
        "tl": "I-tap ang «My credit» tapos «Show my report» — ipadala ang one-time link sa kanila; bubuksan nila para makita ang iyong report. O gamitin ang «Get invite link» at ipadala ang link; pag binuksan nila at nag-message, matatanggap mo ang report.",
    },
    "help_section_report_title": {"en": "📌 Report & appeals", "zh": "📌 举报与申诉", "tl": "📌 Report at appeals"},
    "help_section_report": {
        "en": "**Wrong info?** Use «Report / Incorrect?» on the result card to submit. Check «My appeals» to see status (pending/processed/rejected).",
        "zh": "**信息有误？** 在结果卡片上点「举报/纠错」提交。在「我的申诉」中可查看状态（待处理/已处理/已驳回）。",
        "tl": "**Maling info?** Gamitin ang «Report / Incorrect?» sa result card. Sa «My appeals» makikita ang status.",
    },
    "help_section_data_title": {"en": "📌 Data notice", "zh": "📌 数据说明", "tl": "📌 Paunawa sa data"},
    "help_section_data": {
        "en": "Data: user reports + authorized data + public chain. For fraud prevention only. Reference only; trade with caution.",
        "zh": "数据来源：用户举报 + 授权数据 + 公开链上，仅用于防诈。仅供参考，交易需谨慎。",
        "tl": "Data: user reports + authorized data + public chain. Para sa fraud prevention. Reference lang; mag-ingat sa trade.",
    },
    "help_body": {
        "en": (
            "**Commands**\n"
            "/check <ID|@user|wallet> — Check someone's risk & score\n"
            "Or **forward a message** from that user here — I'll look them up automatically\n"
            "/invite — When they hide their info: get a link for them to open & message; you get their report\n"
            "/mycredit — Check your own credit\n"
            "/help — This message\n\n"
            "**Data source**: User reports + authorized data + public chain. For fraud prevention.\n"
            "**Wrong info?** Use «Report / Incorrect?» on the result card."
        ),
        "zh": (
            "**命令**\n"
            "/check <数字ID|@用户|钱包> — 查询对方风险与信用分\n"
            "或 **转发对方一条消息** 到本机器人，自动查询该用户\n"
            "/invite — 对方隐藏来源时，获取链接让对方点开发消息，报告会发给你\n"
            "/mycredit — 查询自己的信用\n"
            "/help — 本帮助\n\n"
            "**数据来源**：用户举报 + 授权数据 + 公开链上，用于防诈。\n"
            "**信息有误？** 在结果卡片上点「举报/纠错」。"
        ),
        "tl": (
            "**Commands**\n"
            "/check <id|@user|wallet> — Suriin ang risk at score ng iba\n"
            "/mycredit — Suriin ang iyong credit\n"
            "/help — Mensaheng ito\n\n"
            "**Data source**: User reports + authorized data + public chain. Para sa fraud prevention.\n"
            "**Maling info?** Gamitin ang «Report / Incorrect?» sa result card."
        ),
    },
    "report_received": {"en": "✅ Report received. We will review it.", "zh": "✅ 已收到举报，我们会核实。", "tl": "✅ Natanggap ang report. Ire-review namin."},
    "report_usage": {
        "en": "To report: use the «Report / Incorrect?» button on a check result, or send /report after the target ID.",
        "zh": "举报：在查询结果上点「举报/纠错」按钮，或发送 /report 后跟对方 ID。",
        "tl": "Para mag-report: gamitin ang button na «Report / Incorrect?» sa check result.",
    },
    "report_prompt": {
        "en": "Please send your report reason in one message (you may include tx hash or screenshot link).",
        "zh": "请用一条消息发送举报原因（可包含交易哈希或截图链接）。",
        "tl": "Pakipadala ang dahilan ng report sa isang mensahe (maaari kang maglagay ng tx hash o screenshot link).",
    },
    "report_session_expired": {
        "en": "Session expired. Use the Report button on a check result again.",
        "zh": "会话已过期，请重新在查询结果上点击举报按钮。",
        "tl": "Nag-expire na ang session. Gamitin muli ang Report button sa check result.",
    },
    "report_reason_empty": {"en": "Reason cannot be empty.", "zh": "原因不能为空。", "tl": "Ang reason ay hindi pwedeng walang laman."},
    "report_choose_category": {"en": "Choose report type:", "zh": "请选择举报类型：", "tl": "Piliin ang uri ng report:"},
    "report_category_scam": {"en": "Fraud / Scam", "zh": "诈骗", "tl": "Fraud / Scam"},
    "report_category_run_order": {"en": "Run order / No delivery", "zh": "跑单/不发货", "tl": "Run order / Walang delivery"},
    "report_category_lost_contact": {"en": "Lost contact", "zh": "失联", "tl": "Nawala ang contact"},
    "report_category_other": {"en": "Other", "zh": "其他", "tl": "Iba"},
    "section_report_by_category": {"en": "Reports by type (verified)", "zh": "举报分类（已核实）", "tl": "Reports by type (verified)"},
    "section_risk_breakdown": {"en": "Risk source breakdown", "zh": "风险来源占比", "tl": "Risk source breakdown"},
    "report_last_7d": {"en": "Reports in last 7 days", "zh": "近7天举报", "tl": "Reports sa nakaraang 7 araw"},
    "report_last_30d": {"en": "Reports in last 30 days", "zh": "近30天举报", "tl": "Reports sa nakaraang 30 araw"},
    "risk_src_report": {"en": "Reports", "zh": "举报", "tl": "Reports"},
    "risk_src_blacklist": {"en": "Blacklist", "zh": "黑名单", "tl": "Blacklist"},
    "risk_src_high_risk_groups": {"en": "High-risk groups", "zh": "高风险群", "tl": "High-risk groups"},
    "risk_src_new_account": {"en": "New account", "zh": "新账号", "tl": "New account"},
    "risk_src_wallet_anomaly": {"en": "Wallet link", "zh": "钱包关联", "tl": "Wallet link"},
    "section_tx_stats": {"en": "In-app transaction confirmations", "zh": "站内交易确认", "tl": "In-app transaction confirmations"},
    "section_tx_stats_subtitle": {"en": "(Confirmed by both parties in this app; not on-chain flow.)", "zh": "（由交易双方在本应用内确认的笔数/金额，非链上流水。）", "tl": "(Confirmed by both parties in this app; not on-chain flow.)"},
    "tx_stats_line": {"en": "Success: {count} / {total} ({rate}%), total amount: {amount}", "zh": "成功：{count} / {total} 笔（成功率 {rate}%），累计金额：{amount}", "tl": "Success: {count} / {total} ({rate}%), total amount: {amount}"},
    "sharecard_section_tx_label": {"en": "In-app confirmations", "zh": "站内确认", "tl": "In-app confirmations"},
    "sharecard_line_flow_visible": {"en": "On-chain flow: viewable", "zh": "链上流水：可查", "tl": "On-chain flow: viewable"},
    "sharecard_line_flow_verified": {"en": "On-chain flow: viewable (verified)", "zh": "链上流水：可查（已验证）", "tl": "On-chain flow: viewable (verified)"},
    "sharecard_line_flow_unverified": {"en": "On-chain flow: viewable (unverified)", "zh": "链上流水：可查（未验证）", "tl": "On-chain flow: viewable (unverified)"},
    "wallet_flow_privacy_hint": {"en": "If you turn on «Show flow», people who check your report can see this address’s recent on-chain transactions. You can turn it off or unbind anytime. Data: public chain, for reference only.", "zh": "开启「展示流水」后，查你报告的人可看到该地址近期链上流水。可随时关闭或解绑。数据来源：链上公开，仅供参考。", "tl": "If you turn on «Show flow», people who check you can see this address’s on-chain flow. You can turn off or unbind anytime. Data: chain, for reference only."},
    "btn_tx_confirm": {"en": "Request trade confirmation", "zh": "请求对方确认交易", "tl": "Request trade confirmation"},
    "btn_pending_tx": {"en": "Pending confirmations", "zh": "待我确认", "tl": "Pending confirmations"},
    "tx_confirm_prompt": {"en": "Send the amount (e.g. 100 or 100 USDT). Optional: add a memo on the next line.", "zh": "请发送金额（如 100 或 100 USDT）。可选：下一行写备注。", "tl": "Send the amount (e.g. 100 or 100 USDT). Optional: add a memo."},
    "tx_confirm_sent": {"en": "Request sent. The other party will see it under «Pending confirmations».", "zh": "已发送确认请求，对方可在「待我确认」中同意或拒绝。", "tl": "Request sent. They can accept or reject under «Pending confirmations»."},
    "tx_pending_title": {"en": "Someone requested you to confirm a trade", "zh": "对方请求你确认一笔交易", "tl": "Someone requested you to confirm a trade"},
    "tx_pending_line": {"en": "Amount: {amount} {currency}. Tap below to respond.", "zh": "金额：{amount} {currency}。请点击下方按钮回应。", "tl": "Amount: {amount} {currency}. Tap below to respond."},
    "tx_accept": {"en": "Accept", "zh": "同意", "tl": "Accept"},
    "tx_reject": {"en": "Reject", "zh": "拒绝", "tl": "Reject"},
    "tx_accepted": {"en": "You accepted this trade.", "zh": "你已同意这笔交易。", "tl": "You accepted this trade."},
    "tx_rejected": {"en": "You rejected this trade.", "zh": "你已拒绝这笔交易。", "tl": "You rejected this trade."},
    "tx_pending_empty": {"en": "No pending confirmations.", "zh": "暂无待确认的交易。", "tl": "No pending confirmations."},
    "tx_pending_list_header": {"en": "Pending confirmations (tap to respond):", "zh": "待确认的交易（点击回应）：", "tl": "Pending confirmations (tap to respond):"},
    "tx_err_self": {"en": "You cannot request confirmation with yourself.", "zh": "不能向自己请求确认。", "tl": "You cannot request confirmation with yourself."},
    "tx_err_too_many": {"en": "Too many pending requests with this user. Ask them to respond first.", "zh": "该用户待确认请求过多，请先让对方处理。", "tl": "Too many pending. Ask them to respond first."},
    "admin_forbidden": {"en": "⛔ Admin only.", "zh": "⛔ 仅管理员。", "tl": "⛔ Admin lang."},
    "stats_format": {
        "en": "📈 **Stats**\nQueries today: {queries_today}\nTotal users: {total_users}\nBlacklist: {blacklist_count}\nReports pending: {reports_pending}",
        "zh": "📈 **统计**\n今日查询：{queries_today}\n总用户数：{total_users}\n黑名单：{blacklist_count}\n待处理举报：{reports_pending}",
        "tl": "📈 **Stats**\nQueries ngayon: {queries_today}\nTotal users: {total_users}\nBlacklist: {blacklist_count}\nReports pending: {reports_pending}",
    },
    "risk_low": {"en": "Low", "zh": "低", "tl": "Mababa"},
    "risk_medium": {"en": "Medium", "zh": "中", "tl": "Katamtaman"},
    "risk_high": {"en": "High", "zh": "高", "tl": "Mataas"},
    "risk_extreme": {"en": "Extreme", "zh": "极高", "tl": "Sobra"},
    # Tips (from API/scoring - map by key or by English text)
    "tip_new_account": {"en": "New account (<3 months)", "zh": "新账号（不足3个月）", "tl": "Bagong account (<3 buwan)"},
    "tip_high_risk_groups": {"en": "High-risk group exposure ({count})", "zh": "高危群曝光（{count}）", "tl": "High-risk group exposure ({count})"},
    "tip_blacklist": {"en": "Listed on blacklist", "zh": "已列入黑名单", "tl": "Nasa blacklist"},
    "tip_reported_by": {"en": "Reported by {n} user(s)", "zh": "被 {n} 人举报", "tl": "Ni-report ng {n} user(s)"},
    "tip_premium": {"en": "Telegram Premium", "zh": "Telegram Premium", "tl": "Telegram Premium"},
    "tip_no_indicators": {"en": "No special risk indicators", "zh": "无特殊风险指标", "tl": "Walang espesyal na risk indicators"},
    # API messages (shown when no tg_id / wallet has no record)
    "msg_username_not_found": {
        "en": "Username not found or set to private.",
        "zh": "未找到该用户名或已设为私密。",
        "tl": "Hindi mahanap ang username o naka-private.",
    },
    "msg_username_alternative": {
        "en": "**Easier:** Forward a message from that user to this bot — I'll look up their ID and run the check automatically. Or use /check <numeric ID> if you know it.",
        "zh": "**更省事：** 直接转发对方的一条消息到本机器人，我会自动识别并查询该用户信用。若您知道对方数字 ID，也可用 /check 数字ID。",
        "tl": "**Mas madali:** I-forward ang isang mensahe galing sa user na iyon dito — hahanapin ko ang kanilang ID at iche-check. O gamitin ang /check <numeric ID> kung alam mo.",
    },
    "forward_hidden": {
        "en": "Forward source is hidden — I can't get their ID. Ask them to send /mycredit here to see their own score, or use their numeric ID: /check <ID>.",
        "zh": "转发来源已隐藏，无法获取用户 ID。可请对方在本机器人里发送 /mycredit 查看自己的信用，或使用其数字 ID：/check 数字ID。",
        "tl": "Nakatago ang pinagmulan ng forward — hindi makuha ang ID. Pakiusapan silang mag-send ng /mycredit dito, o gamitin ang kanilang numeric ID: /check <ID>.",
    },
    "forward_checking": {
        "en": "Checking the forwarded user…",
        "zh": "正在查询被转发消息的用户…",
        "tl": "Sinusuri ang na-forward na user…",
    },
    "msg_wallet_no_record": {
        "en": "No record for this wallet. No record does not mean safe.",
        "zh": "该钱包暂无记录。无记录不代表安全。",
        "tl": "Walang record para sa wallet na ito. Ang walang record ay hindi nangangahulugang safe.",
    },
    "invite_btn": {"en": "🔗 Get invite link", "zh": "🔗 获取邀请链接", "tl": "🔗 Kunin ang invite link"},
    "error_invite_connection": {
        "en": "Cannot reach the server. Please start the API first (run run_api.py), then try again.",
        "zh": "无法连接服务。请先启动 API（运行 run_api.py），再重试。",
        "tl": "Hindi maabot ang server. Pakistart muna ang API (run_api.py), saka subukan muli.",
    },
    "invite_click_here": {"en": "Click the button below to get your link.", "zh": "点击下方按钮获取专属链接。", "tl": "I-click ang button sa ibaba para makuha ang link."},
    "invite_instructions": {
        "en": "Send the link below to the person you want to check. They must open it and send any message to this bot within {minutes} minutes. Then their credit report will be sent to you here.",
        "zh": "请把下面的链接发给要查的人。对方需在 {minutes} 分钟内点击链接并给本机器人发任意一条消息，我们就会把该用户的信用报告发给你。",
        "tl": "I-send ang link sa taong gusto mong i-check. Dapat i-open nila at mag-send ng kahit anong mensahe dito sa bot sa loob ng {minutes} minuto. Pagkatapos, ang kanilang credit report ay ipapadala sa iyo.",
    },
    "invite_link_label": {"en": "Invite link (valid {minutes} min):", "zh": "邀请链接（{minutes} 分钟内有效）：", "tl": "Invite link (valid {minutes} min):"},
    "invite_expired": {
        "en": "This invite link has expired. Ask the person who wanted to check you to get a new link from the bot.",
        "zh": "该邀请链接已过期。请让查询方重新在机器人里获取新链接。",
        "tl": "Nag-expire na ang invite link na ito. Pakiusapan ang nag-request ng bagong link mula sa bot.",
    },
    "invite_done_to_target": {
        "en": "✅ Your credit report has been sent to the person who invited you.",
        "zh": "✅ 您的信用报告已发送给邀请方。",
        "tl": "✅ Ang iyong credit report ay naipadala na sa nag-anyaya sa iyo.",
    },
    "invite_report_title_to_querier": {
        "en": "📋 Credit report of the user you invited:",
        "zh": "📋 您邀请的用户的信用报告如下：",
        "tl": "📋 Credit report ng user na inanyayahan mo:",
    },
    "report_identity_title": {"en": "👤 Identified user", "zh": "👤 被查用户", "tl": "👤 User na na-check"},
    "report_no_label": {"en": "Report No.", "zh": "报告编号", "tl": "Report No."},
    "report_generated_label": {"en": "Generated", "zh": "生成时间", "tl": "Generated"},
    "report_time_utc": {"en": " (UTC)", "zh": " (UTC)", "tl": " (UTC)"},
    "report_usage_hint": {"en": "This report can be used for verification, record-keeping, or asking the other party to show their report.", "zh": "本报告可用于对质、存证或要求对方出示报告。", "tl": "Ang report na ito ay maaaring gamitin para sa verification, record, o paghingi ng report sa kabila."},
    "no_tags_placeholder": {"en": "—", "zh": "—", "tl": "—"},
    "section_credit_result": {"en": "📊 Credit conclusion", "zh": "📊 信用结论", "tl": "📊 Credit conclusion"},
    "report_user_id": {"en": "User ID", "zh": "用户ID", "tl": "User ID"},
    "report_username": {"en": "Username", "zh": "用户名", "tl": "Username"},
    "report_display_name": {"en": "Display name", "zh": "用户名称", "tl": "Display name"},
    "msg_username_alternative_invite": {
        "en": "**No ID & they hid their info?** Get an invite link and send it to them. When they open it and send a message here, their report goes to you.",
        "zh": "**不知道 ID 且对方隐藏了信息？** 获取专属链接发给对方。对方点开并给机器人发一条消息后，我们会把其信用报告直接发给你。",
        "tl": "**Walang ID at nakatago ang info nila?** Kumuha ng invite link at ipadala sa kanila. Kapag binuksan nila at nag-send dito, ang report ay mapapasa sa iyo.",
    },
    "forward_hidden_invite": {
        "en": "**Forward source is hidden.** Get a link and send it to that person; when they open it and message the bot, you'll get their report here.",
        "zh": "**转发来源已隐藏。** 请获取专属链接并发给对方；对方点开并发一条消息后，我们会把其信用报告发给你。",
        "tl": "**Nakatago ang forward source.** Kumuha ng link at ipadala sa taong iyon; kapag binuksan nila at nag-message sa bot, makukuha mo ang report dito.",
    },
}


def get_locale(language_code: str | None) -> str:
    """Normalize Telegram language_code to supported locale (en, zh, tl)."""
    if not language_code:
        return DEFAULT_LOCALE
    code = (language_code or "").strip().lower()
    if code.startswith("zh"):
        return "zh"
    if code in ("tl", "fil"):
        return "tl"
    if code == "en" or code.startswith("en-"):
        return "en"
    return DEFAULT_LOCALE


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Get translated string for key. lang defaults to en."""
    locale = (lang or DEFAULT_LOCALE) if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    by_locale = TEXTS.get(key, {})
    s = by_locale.get(locale) or by_locale.get(DEFAULT_LOCALE) or key
    if kwargs:
        try:
            s = s.format(**kwargs)
        except KeyError:
            pass
    return s


def get_texts_for_key(key: str) -> list[str]:
    """All translations for a key (for matching button/label in any language)."""
    by_locale = TEXTS.get(key, {})
    return [by_locale[loc] for loc in SUPPORTED_LOCALES if by_locale.get(loc)]


# Map API tip strings (English) to i18n key + optional params for translation
def translate_tip(tip: str, lang: str) -> str:
    """Convert one tip string from API to localized string."""
    tip = (tip or "").strip()
    if not tip:
        return t("tip_no_indicators", lang=lang)
    if "New account" in tip or "new account" in tip.lower():
        return t("tip_new_account", lang=lang)
    if "High-risk group" in tip or "high-risk" in tip.lower():
        m = re.search(r"\((\d+)\)", tip)
        count = m.group(1) if m else "0"
        return t("tip_high_risk_groups", lang=lang, count=count)
    if "blacklist" in tip.lower() or "Listed on" in tip:
        return t("tip_blacklist", lang=lang)
    if "Reported by" in tip:
        m = re.search(r"(\d+)", tip)
        n = m.group(1) if m else "0"
        return t("tip_reported_by", lang=lang, n=n)
    if "Premium" in tip:
        return t("tip_premium", lang=lang)
    if "No special" in tip or "no special" in tip.lower():
        return t("tip_no_indicators", lang=lang)
    return tip
