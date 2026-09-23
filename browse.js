const { chromium } = require('playwright');
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

(async () => {
  const b = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"] });
  const ctx = await b.newContext({ userAgent: UA, viewport: { width: 1366, height: 900 }, locale: "en-US" });
  await ctx.addInitScript(() => { Object.defineProperty(navigator, "webdriver", { get: () => undefined }); });
  const p = await ctx.newPage();

  console.log("goto search page...");
  await p.goto("https://www.tiktok.com/search/video?q=%23capcutnow", { waitUntil: "domcontentloaded", timeout: 60000 });
  await p.waitForTimeout(6000);
  console.log("page title:", await p.title());

  for (const t of ["Accept all", "Allow all", "Decline all", "Refuse all"]) {
    try { await p.click(`button:has-text("${t}")`, { timeout: 1500 }); console.log("cookie banner:", t); break; } catch (e) {}
  }

  try { await p.click("text=Filters", { timeout: 5000 }); await p.waitForTimeout(1500); console.log("filters panel opened"); } catch (e) { console.log("filters btn: none"); }
  const applied = [];
  for (const t of ["Most recent", "This week", "Upload date", "Latest"]) {
    try { await p.click(`text=${t}`, { timeout: 2000 }); applied.push(t); await p.waitForTimeout(1200); } catch (e) {}
  }
  console.log("applied filters:", applied.join(",") || "none");
  await p.waitForTimeout(6000);

  const cards = await p.$$eval('a[href*="/video/"]', as => as.slice(0, 15).map(a => ({
    href: a.href,
    txt: (a.innerText || "").replace(/\n+/g, " | ").slice(0, 110),
  })));
  console.log("RESULT CARDS:", JSON.stringify(cards, null, 1));

  const s = await p.content();
  console.log("captcha hint:", /captcha|Slide to verify|whirl/i.test(s));
  console.log("login wall hint:", /Log in to TikTok/i.test(s));
  await b.close();
})().catch(e => { console.error("ERR:", e.message); process.exit(1); });
