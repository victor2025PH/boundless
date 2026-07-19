/**
 * ============================================================================
 * 开场页「进入 AI 世界」按钮（Siri 流光改造）—— Playwright 验收脚本
 * ============================================================================
 *
 * 用途：
 *   首页开场页的 .bl-enter-btn 按钮改造为 Siri 风格多彩流光按钮后，本脚本
 *   用无头浏览器对它的 DOM 结构与交互行为做验收，共 6 项检查（按执行顺序）：
 *
 *   dom-structure   按钮内含 .siri-halo（呼吸光晕）/ .siri-ring（描边容器）/
 *                   .siri-ring .flow（旋转 conic 渐变流光）/ .siri-glass
 *                   （玻璃高光层）/ .label / .arrow 六个子元素
 *   hover-css-vars  指针在按钮上移动时，内联样式更新 --mx/--my（百分比）与
 *                   --rx/--ry（deg，3D 倾斜）；左上/右下两处采样均非空且不同
 *   press-charging  pointerdown 时按钮获得 data-charging="1"，pointerup 后清除
 *   intro-stats     回归：window.__blIntroStats 存在（LOGO 粒子引擎仍在跑）；
 *                   放在点击检查之前，避免开场页退场后全局对象被清理
 *   click-warping   点击按钮后 300ms 内 #bl-intro 的 class 包含 warping；
 *                   点击会让开场页退场，故此项放在最后
 *   console-errors  控制台错误数为 0（收集 console error 与 pageerror，忽略
 *                   Next dev 已知水合警告 "Extra attributes from the server"）
 *
 * 前置：
 *   先启动站点服务（npm run dev 或 npm run build && npm run start），
 *   确保 --url 指向的地址可访问。
 *
 * 用法：
 *   node scripts/qa-intro-button.mjs
 *   node scripts/qa-intro-button.mjs --url http://localhost:3470/
 *
 * 输出：
 *   每项检查实时打印一行 {name, pass, detail}，最终打印格式化 JSON
 *   {url, checks, pass}。
 *
 * 退出码：0 = 全部通过；1 = 有失败项或脚本异常；2 = 按钮 20s 内未可见。
 * ============================================================================
 */

import { chromium } from 'playwright';

// ---------- CLI 参数（支持 `--url x` 与 `--url=x` 两种写法） ----------
function getArg(name, fallback) {
  const argv = process.argv.slice(2);
  const eq = argv.find((a) => a.startsWith(`--${name}=`));
  if (eq) return eq.slice(name.length + 3);
  const i = argv.indexOf(`--${name}`);
  if (i !== -1 && argv[i + 1] && !argv[i + 1].startsWith('--')) return argv[i + 1];
  return fallback;
}

const url = getArg('url', 'http://localhost:3470/');

// Next dev 已知水合警告，不计入控制台错误
const IGNORED_CONSOLE_PATTERNS = ['Extra attributes from the server'];

const checks = [];
function record(name, pass, detail) {
  checks.push({ name, pass, detail });
  console.log(JSON.stringify({ name, pass, detail }));
}

const browser = await chromium.launch({ headless: true });

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });

  // ---------- 全程收集控制台错误与页面异常（最后统一断言） ----------
  const consoleErrors = [];
  const ignored = (text) => IGNORED_CONSOLE_PATTERNS.some((p) => text.includes(p));
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !ignored(msg.text())) {
      consoleErrors.push(`console.error: ${msg.text()}`);
    }
  });
  page.on('pageerror', (err) => {
    const text = err?.message ?? String(err);
    if (!ignored(text)) consoleErrors.push(`pageerror: ${text}`);
  });

  // 钉死 intro_auto_enter 实验为对照桶，避免 B 桶自动进入干扰交互检查的时序
  await page.addInitScript(() => {
    try {
      localStorage.setItem('ab_intro_auto_enter', 'a');
    } catch {}
  });

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });

  const btn = page.locator('.bl-enter-btn');
  try {
    await btn.waitFor({ state: 'visible', timeout: 20000 });
  } catch {
    console.error(`[qa] .bl-enter-btn 未在 20s 内可见（url: ${url}），请确认服务已启动且开场页正常渲染。`);
    await browser.close();
    process.exit(2);
  }

  // 等入场动画完成
  await page.waitForTimeout(1500);

  // 点击后按钮可能随开场页退场，持有 ElementHandle 保证后续仍可读取属性
  const handle = await btn.elementHandle();
  const box = await btn.boundingBox();
  if (!handle || !box) throw new Error('无法获取 .bl-enter-btn 的元素句柄或布局信息');

  const readVars = () =>
    handle.evaluate((el) => ({
      mx: el.style.getPropertyValue('--mx').trim(),
      my: el.style.getPropertyValue('--my').trim(),
      rx: el.style.getPropertyValue('--rx').trim(),
      ry: el.style.getPropertyValue('--ry').trim(),
    }));

  // ---------- 检查 1：DOM 结构 ----------
  {
    // siri-shape/siri-blob/bl-wave-defs：波浪形变层（湍流滤镜 + blob 底面）
    const required = [
      '.siri-halo',
      '.siri-shape',
      '.siri-blob',
      '.siri-ring',
      '.siri-ring .flow',
      '.siri-glass',
      '.bl-wave-defs filter feDisplacementMap',
      '.label',
      '.arrow',
    ];
    const missing = await handle.evaluate((el, sels) => sels.filter((s) => !el.querySelector(s)), required);
    record(
      'dom-structure',
      missing.length === 0,
      missing.length === 0 ? `${required.length} 个子元素齐全` : `缺少子元素: ${missing.join(', ')}`
    );
  }

  // ---------- 检查 2：指针跟随 CSS 变量（--mx/--my/--rx/--ry） ----------
  {
    await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.25, { steps: 8 });
    await page.waitForTimeout(200);
    const first = await readVars();

    await page.mouse.move(box.x + box.width * 0.75, box.y + box.height * 0.75, { steps: 8 });
    await page.waitForTimeout(200);
    const second = await readVars();

    const keys = ['mx', 'my', 'rx', 'ry'];
    const empties = keys.filter((k) => !first[k] || !second[k]);
    const unchanged = keys.filter((k) => first[k] === second[k]);
    const pass = empties.length === 0 && unchanged.length === 0;
    record(
      'hover-css-vars',
      pass,
      pass
        ? `左上采样 ${JSON.stringify(first)} → 右下采样 ${JSON.stringify(second)}`
        : `${empties.length ? `为空: ${empties.map((k) => `--${k}`).join(', ')}；` : ''}` +
          `${unchanged.length ? `两次相同: ${unchanged.map((k) => `--${k}`).join(', ')}；` : ''}` +
          `读值 ${JSON.stringify(first)} / ${JSON.stringify(second)}`
    );
  }

  // ---------- 检查 3：按压充能（data-charging） ----------
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  {
    await page.mouse.move(cx, cy, { steps: 4 });
    await page.mouse.down();
    await page.waitForTimeout(150);
    const whileDown = await handle.evaluate((el) => el.getAttribute('data-charging'));

    // 注意：同一位置 down+up 会派发一次 click，可能已提前触发冲越
    await page.mouse.up();
    await page.waitForTimeout(150);
    const afterUp = await handle.evaluate((el) => el.getAttribute('data-charging'));

    const pass = whileDown === '1' && afterUp !== '1';
    record(
      'press-charging',
      pass,
      `按下时 data-charging=${JSON.stringify(whileDown)}（期望 "1"），松开后=${JSON.stringify(afterUp)}（期望清除）`
    );
  }

  // ---------- 检查 4：粒子引擎回归（须在开场页退场前读取） ----------
  {
    const stats = await page.evaluate(() => {
      const s = window.__blIntroStats;
      return { exists: typeof s !== 'undefined' && s !== null, type: typeof s };
    });
    record(
      'intro-stats',
      stats.exists,
      stats.exists ? `window.__blIntroStats 存在（typeof: ${stats.type}）` : 'window.__blIntroStats 不存在'
    );
  }

  // ---------- 检查 5：点击冲越（#bl-intro 带上 warping，放最后） ----------
  {
    const hasWarping = () =>
      page.evaluate(() => {
        const intro = document.querySelector('#bl-intro');
        return intro ? intro.classList.contains('warping') : null;
      });

    let pass = false;
    let detail;
    if ((await hasWarping()) === true) {
      // 按压检查的 down+up 已构成一次 click，冲越已被触发，视为通过
      pass = true;
      detail = '按压检查的 down+up 已触发点击，#bl-intro 已带 warping';
    } else {
      await page.mouse.click(cx, cy);
      const start = Date.now();
      let state = null;
      while (Date.now() - start <= 300) {
        state = await hasWarping();
        if (state === true) break;
        await page.waitForTimeout(25);
      }
      pass = state === true;
      detail =
        state === true
          ? `点击后 ${Date.now() - start}ms 内 #bl-intro 带上 warping`
          : state === null
            ? '未找到 #bl-intro 容器'
            : '点击后 300ms 内 #bl-intro 未出现 warping';
    }
    record('click-warping', pass, detail);
  }

  // ---------- 检查 6：控制台错误 ----------
  // 稍等片刻，让点击后的异步报错有机会浮出
  await page.waitForTimeout(400);
  {
    const pass = consoleErrors.length === 0;
    record(
      'console-errors',
      pass,
      pass
        ? '无控制台错误（已忽略 "Extra attributes from the server" 水合警告）'
        : `共 ${consoleErrors.length} 条错误，例如: ${consoleErrors.slice(0, 3).join(' | ')}`
    );
  }

  // ---------- 汇总输出 ----------
  const pass = checks.every((c) => c.pass);
  console.log(JSON.stringify({ url, checks, pass }, null, 2));
  await browser.close();
  process.exit(pass ? 0 : 1);
} catch (err) {
  console.error('[qa] 脚本执行异常（请确认本地服务已启动）:', err?.message ?? err);
  if (checks.length) console.log(JSON.stringify({ url, checks, pass: false }, null, 2));
  await browser.close().catch(() => {});
  process.exit(1);
}
