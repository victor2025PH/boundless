# WhatsApp Cloud API · 24h 窗口回退模板（提交 Meta 用）

客户超过 24 小时没有回复时，Cloud API 拒收自由文本（错误码 131047，`window_expired`）。窗口内仍发自由文本；窗口外只发**已经审核通过**的模板。

回退是被动的：`wa_send_text` 被拒成 `window_expired` 时，才改发这里的模板。没有「客户沉默满 24 小时」的定时外发，本变更也不加这种任务。下一次人工或自动回复撞上窗口过期，就会走同一条回退。单独做沉默满 24 小时的群发，形态更接近 MARKETING，会伤号码质量评级，要另立项，并且复用已审核的 UTILITY 模板，不能用这批正文去扫沉默客户。

审核通过之前不要把 `name` 填进 `whatsapp_cloud.window_fallback_template`。示例配置的名字是空的，复制过去不会发出未审核模板。

## 提交口径

| 项 | 约定 |
| --- | --- |
| 类别 | `UTILITY`（客服跟进，不是促销） |
| 正文变量 | 恰好一个 `{{1}}`，不在开头或结尾；样例见各条 `samples.1` |
| 参数 | 简短事项，最多 80 字；换行会被折成空格。不要把整段聊天或链接塞进 `{{1}}` |
| 语言 | 会话 `zh` → 模板语言 `zh_CN`；`en`（含 `en-PH`）→ `en`；`tl` / `fil` / Taglish / `ceb` → `fil`（Meta 没有单独的 Tagalog / Taglish 码） |
| 退订 | 中文整句回复「退订」；英 / 他加禄整句回复 `STOP`。与现有 STOP 闸的整句关键词一致，停联后文本和模板都不再发 |
| 公开包 | 跨境私域、代运营、自有业务三套中性模板 |
| 内部包 | `wa_fb_gamble_*` 只在 `config/presets/internal/gambling_operator/`。公开包省略该目录；`CHATX_FLAVOR` / `CHATX_EDITION` 为 `public` 或 `clean` 时选择器丢掉此前缀 |

填配置（审核通过后，按本号所属行业包挑一套）：

```yaml
whatsapp_cloud:
  window_fallback_template:
    name: ""
    language: zh_CN
    text_param: true
    # default_language 不再参与选择。未知语言不发模板。
    by_language:
      zh: {name: cbp_svc_followup_zh, language: zh_CN, text_param: true, param_max_chars: 80}
      en: {name: cbp_svc_followup_en, language: en, text_param: true, param_max_chars: 80}
      tl: {name: cbp_svc_followup_fil, language: fil, text_param: true, param_max_chars: 80}
```

只填顶层 `name` / `language` / `text_param`、不写 `by_language` 时，行为与旧的单模板一样，不看会话语言。写了 `by_language` 之后，会话语言未知、为空，或不在 zh/en/tl：名字为空，不发。成功时响应里的 `fallback_lang` 是命中的档，`fallback_template_lang` 是发给 Meta 的语言码。

## 公开包 · 跨境私域 `cross_border_private`

| name | category | lang | language | {{1}} 样例 |
| --- | --- | --- | --- | --- |
| `cbp_svc_followup_zh` | UTILITY | zh | zh_CN | 订单物流 |
| `cbp_svc_followup_en` | UTILITY | en | en | your order status |
| `cbp_svc_followup_fil` | UTILITY | tl | fil | order status |

`cbp_svc_followup_zh` / UTILITY / zh_CN

```
您好，关于您之前咨询的{{1}}，需要您回复后我们才能继续处理。请直接回复本消息，客服会接着帮您跟进。如不需要再收到消息，请回复「退订」。
```

`cbp_svc_followup_en` / UTILITY / en

```
Hello, about your earlier question on {{1}}, we can continue only after you reply. Please reply to this message and we will follow up on your request. Reply STOP to opt out.
```

`cbp_svc_followup_fil` / UTILITY / fil

```
Kumusta po. Tungkol sa tanong ninyo kanina tungkol sa {{1}}, kailangan po ninyong sumagot para maipagpatuloy namin. Paki-reply lang po sa mensaheng ito. Kung ayaw na po tumanggap ng mensahe, i-reply ang STOP.
```

## 公开包 · 代运营 `agency`

| name | category | lang | language | {{1}} 样例 |
| --- | --- | --- | --- | --- |
| `agy_svc_followup_zh` | UTILITY | zh | zh_CN | 售后问题 |
| `agy_svc_followup_en` | UTILITY | en | en | your support request |
| `agy_svc_followup_fil` | UTILITY | tl | fil | support request |

`agy_svc_followup_zh` / UTILITY / zh_CN

```
您好，关于您咨询的{{1}}，客服需要您回复后才能继续协助。请直接回复本消息。如不需要再收到消息，请回复「退订」。
```

`agy_svc_followup_en` / UTILITY / en

```
Hello, regarding {{1}} from your last message, we can keep helping only after you reply. Please reply to this message. Reply STOP to opt out.
```

`agy_svc_followup_fil` / UTILITY / fil

```
Kumusta po. Tungkol sa {{1}} na tinanong ninyo, magpapatuloy lang po kami kapag sumagot kayo. Paki-reply po sa mensaheng ito. Kung ayaw na po ng mensahe, i-reply ang STOP.
```

## 公开包 · 自有业务 `own_business`

| name | category | lang | language | {{1}} 样例 |
| --- | --- | --- | --- | --- |
| `own_svc_followup_zh` | UTILITY | zh | zh_CN | 预约时间 |
| `own_svc_followup_en` | UTILITY | en | en | your appointment |
| `own_svc_followup_fil` | UTILITY | tl | fil | appointment |

`own_svc_followup_zh` / UTILITY / zh_CN

```
您好，关于您之前说的{{1}}，我们先记下了。请直接回复本消息，我们就能继续为您安排。如不需要再收到消息，请回复「退订」。
```

`own_svc_followup_en` / UTILITY / en

```
Hello, we noted your earlier request about {{1}}. Please reply to this message so we can continue assisting you. Reply STOP to opt out.
```

`own_svc_followup_fil` / UTILITY / fil

```
Kumusta po. Naitala po namin ang hiling ninyo tungkol sa {{1}}. Paki-reply po sa mensaheng ito para maipagpatuloy ang tulong. Kung ayaw na po tumanggap ng mensahe, i-reply ang STOP.
```

## 内部包 · 持牌博彩运营商 `gambling_operator`

只提交给内部号的 WABA。公开安装包不带这些正文。类别仍是 UTILITY：会员服务跟进与账户暂停协助，不写下注、优惠或链接。年龄与账户问题转人工。

| name | category | lang | language | {{1}} 样例 |
| --- | --- | --- | --- | --- |
| `wa_fb_gamble_member_zh` | UTILITY | zh | zh_CN | 账户协助 |
| `wa_fb_gamble_member_en` | UTILITY | en | en | your account request |
| `wa_fb_gamble_member_fil` | UTILITY | tl | fil | account request |
| `wa_fb_gamble_pause_zh` | UTILITY | zh | zh_CN | 暂停账户 |
| `wa_fb_gamble_pause_en` | UTILITY | en | en | pause request |
| `wa_fb_gamble_pause_fil` | UTILITY | tl | fil | pause request |

`wa_fb_gamble_member_zh` / UTILITY / zh_CN

```
您好，会员服务跟进您之前的咨询：{{1}}。请直接回复本消息，我们才能继续处理；年龄与账户问题会转人工核实。如不需要再收到消息，请回复「退订」。
```

`wa_fb_gamble_member_en` / UTILITY / en

```
Hello, member services is following up on {{1}}. Please reply to this message so we can continue. Questions about age or the account go to a person. Reply STOP to opt out.
```

`wa_fb_gamble_member_fil` / UTILITY / fil

```
Kumusta po. Nagfo-follow up ang member services tungkol sa {{1}}. Paki-reply po sa mensaheng ito para maipagpatuloy. Ang tanong sa edad o account ay iaasa sa tao. Kung ayaw na po ng mensahe, i-reply ang STOP.
```

`wa_fb_gamble_pause_zh` / UTILITY / zh_CN

```
您好，您之前申请的账户暂停协助（{{1}}）还没处理完。请直接回复本消息，我们会转人工继续办。如不需要再收到消息，请回复「退订」。
```

`wa_fb_gamble_pause_en` / UTILITY / en

```
Hello, your earlier request to pause the account ({{1}}) is still open. Please reply to this message and a person will continue it. Reply STOP to opt out.
```

`wa_fb_gamble_pause_fil` / UTILITY / fil

```
Kumusta po. Bukas pa po ang hiling ninyong i-pause ang account ({{1}}). Paki-reply po sa mensaheng ito at ipapasa namin sa tao. Kung ayaw na po ng mensahe, i-reply ang STOP.
```
