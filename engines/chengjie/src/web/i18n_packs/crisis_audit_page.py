# -*- coding: utf-8 -*-
"""危机审计页增量词条(2026-08-18 P1 空态改造)。

存量 ca_* 键仍在 web_i18n 单体(词条单源规则:新增进 pack,存量待迁);本包只放
「安全体检」空态三键——生产常态是 0 危机事件,裸空表看着像页面坏了,空态要把
「没有事件」讲成「守卫在工作」的好消息。
"""

ZH = {
    "ca_js010": "没有待处理的危机事件——这是好消息",
    "ca_js011": "查看已处理历史",
    "ca_js012": "危机识别守卫在每条进站消息上运行；一旦命中会在这里留痕，并点亮侧栏红色徽标。",
}

EN = {
    "ca_js010": "No unhandled crisis events — that's good news",
    "ca_js011": "Show handled history",
    "ca_js012": "The crisis-detection guard runs on every inbound message; any hit is recorded here and lights up the red sidebar badge.",
}
