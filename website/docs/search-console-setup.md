# 搜索引擎站长工具提交指引（Google / Naver / Bing）

> 背景：站点已接入 IndexNow（Bing、Naver、Seznam 等即时收录通知），但 **Google 不支持 IndexNow**，
> 必须通过 Google Search Console（GSC）提交。GSC/Naver 后台还能看到搜索词、展示量、点击率，
> 是后续 SEO 迭代的核心数据来源。
>
> 网站代码已内置验证支持：把令牌写进 VPS 的 `.env.local` 即可输出验证元标签，无需改代码。
> 生产域名为 `https://bd2026.cc`（www 子域已在 nginx 层 301 到裸域，提交时一律用裸域）。

## 一、Google Search Console（约 5 分钟）

1. 打开 <https://search.google.com/search-console>，用 Google 账号登录
2. 「添加资源」→ 选 **网址前缀**，填 `https://bd2026.cc`（不要带 www）
3. 验证方式选 **HTML 标记**，会得到一段：
   `<meta name="google-site-verification" content="XXXXXXXX" />`
   把 `content` 里的 `XXXXXXXX` 发给 AI 助手（或自己按下方"令牌配置"操作）
4. 令牌部署生效后，回 GSC 点「验证」
5. 验证通过后：左侧「站点地图」→ 填 `sitemap.xml` → 提交
6. （可选加速）左侧顶部网址检查框逐个输入以下 URL →「请求编入索引」：
   - `https://bd2026.cc/`
   - `https://bd2026.cc/en`
   - `https://bd2026.cc/voice`
   - `https://bd2026.cc/ko/voice`
   - `https://bd2026.cc/ja/voice`

## 二、Naver Search Advisor（韩语市场，约 5 分钟）

1. 打开 <https://searchadvisor.naver.com>，需要 Naver 账号（手机号可注册）
2. 「웹마스터 도구」→ 사이트 등록 → 填 `https://bd2026.cc`
3. 验证方式选 **HTML 태그**（HTML 标签），得到：
   `<meta name="naver-site-verification" content="YYYYYYYY" />`
   同样把 `YYYYYYYY` 交给 AI 助手配置
4. 验证通过后：요청（请求）→ 사이트맵 제출（提交站点地图）→ 填 `https://bd2026.cc/sitemap.xml`
5. 웹 페이지 수집（网页收集）里可单独请求收录 `https://bd2026.cc/ko/voice`

## 三、Bing Webmaster Tools（顺手，约 1 分钟）

GSC 验证完成后：打开 <https://www.bing.com/webmasters> → 选择 **从 GSC 导入**，
一键继承验证和 sitemap，无需重复操作。（Bing 收录本身已有 IndexNow 在推，
这一步主要为了拿 Bing 的搜索分析面板。）

## 令牌配置（AI 助手可代办）

在 VPS `/home/ubuntu/yuntech/.env.local` 追加：

```bash
GOOGLE_SITE_VERIFICATION=XXXXXXXX
NAVER_SITE_VERIFICATION=YYYYYYYY
```

**注意：令牌在构建时内联进静态页面，改完必须重新构建**
（`cd /home/ubuntu/yuntech && npm run build && pm2 restart yuntech --update-env`），
只重启进程不生效。
验证通过后令牌**永久保留**（两家都会周期性复查，删掉会掉验证）。

## 提交后看什么

- 一般 2~7 天开始出现收录数据；GSC「效果」页看搜索词与点击
- 每周运营周报已含 SEO 巡检（sitemap 健康 / 关键页状态码 / IndexNow 推送）
  和 A/B 实验结论（样本达标自动判胜负）
- hreflang 已配置（zh-CN / en / ko / ja），Google 会自动把对应语言页面分发给对应地区用户
