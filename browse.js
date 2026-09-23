const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

async function harvest(p, label) {
  const cards = await p.$$eval('a[href*="/video/"]', as => as.slice(0, 12).map(a => ({
    href: a.href.split("?")[0],
    txt: (a.innerText || "").replace(/\n+/g, " | ").slice(0, 100),
  }))).catch(() => []);
  console.log(`[${label}] cards: ${cards.length}`);
  cards.slice(0, 8).forEach(c => console.log(`[${label}] ${c.href}  << ${c.txt}`));
  return cards.length;
}

(async () => {
  const b = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"] });
  const ctx = await b.newContext({ userAgent: UA, viewport: { width: 1366, height: 900 }, locale: "en-US" });
  await ctx.addInitScript(() => { Object.defineProperty(navigator, "webdriver", { get: () => undefined }); });
  const p = await ctx.newPage();

  console.log("== try 1: search page ==");
  await p.goto("https://www.tiktok.com/search/video?q=%23capcutnow", { waitUntil: "domcontentloaded", timeout: 60000 });
  await p.waitForTimeout(7000);
  let s = await p.content();
  const blocked = /captcha|Slide to verify|whirl/i.test(s) || /Log in to TikTok/i.test(s);
  console.log("search blocked:", blocked);
  if (!blocked) {
    try { await p.click("text=Filters", { timeout: 4000 }); await p.waitForTimeout(1500); } catch (e) {}
    for (const t of ["Most recent", "This week"]) { try { await p.click(`text=${t}`, { timeout: 2000 }); await p.waitForTimeout(1500); } catch (e) {} }
    await harvest(p, "search");
  }

  console.log("== try 2: tag page hydration ==");
  await p.goto("https://www.tiktok.com/tag/capcutnow", { waitUntil: "domcontentloaded", timeout: 60000 });
  await p.waitForTimeout(8000);
  for (let i = 0; i < 3; i++) { await p.mouse.wheel(0, 2500); await p.waitForTimeout(3000); }
  s = await p.content();
  console.log("tag page captcha:", /captcha|Slide to verify|whirl/i.test(s));
  await harvest(p, "tag");

  await b.close();
  console.log("DONE");
})().catch(e => { console.error("ERR:", e.message); process.exit(1); });
