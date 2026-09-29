#!/usr/bin/env node
// G17: сортировка по жалобам в настоящем браузере (headless Chrome).
//   node tools/check_sort.js                   # проверить собранный out/ (поднимает свой сервер)
//   SITE_OUT=/opt/zhk-site/out.test node ...   # проверить пробную сборку
// Печатает SORT_UI_OK только если прошло всё.
const http = require("http");
const fs = require("fs");
const path = require("path");

const OUT = process.env.SITE_OUT || "/opt/zhk-site/out";
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                ".json": "application/json" };
const fail = (m) => { console.log("FAIL: " + m); process.exit(1); };

function serve() {
  return new Promise((ok) => {
    const s = http.createServer((req, res) => {
      let p = path.join(OUT, decodeURIComponent(req.url.split("?")[0]));
      if (p.endsWith("/")) p += "index.html";
      if (!p.startsWith(OUT) || !fs.existsSync(p)) { res.writeHead(404); return res.end(); }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(p)] || "application/octet-stream" });
      fs.createReadStream(p).pipe(res);
    });
    s.listen(0, "127.0.0.1", () => ok(s));
  });
}

// Видимые строки таблицы: значение сортировки, число сообщений, «маленький» ли ЖК.
const visible = (page) => page.$$eval("#zhk-table tbody tr", (trs) => trs.filter((t) => !t.hidden).map((t) => ({
  m: +t.dataset.m, v: t.dataset.v.split(",").map(Number), small: t.hasAttribute("data-small"),
  share: (t.querySelector("td[data-share]") || {}).textContent })));

function monotone(vals, dir) {
  for (let i = 1; i < vals.length; i++) {
    if (dir === "asc" ? vals[i] < vals[i - 1] : vals[i] > vals[i - 1]) return i;
  }
  return 0;
}

(async () => {
  const puppeteer = (await import("/usr/local/lib/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js")).default;
  const srv = await serve();
  const base = `http://127.0.0.1:${srv.address().port}`;
  const browser = await puppeteer.launch({ executablePath: "/usr/bin/google-chrome", headless: "new",
                                           args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1280, height: 900 });

  for (const city of ["sankt-peterburg", "moskva"]) {
    await page.goto(`${base}/${city}/`, { waitUntil: "networkidle0" });
    const keys = await page.$eval("#zhk-table", (t) => t.dataset.keys.split(","));
    const total = (await visible(page)).length;
    if (await page.$eval("[data-sorter]", (b) => b.hidden)) fail(`${city}: переключатель скрыт`);
    for (const key of ["msgs", ...keys]) {
      for (const dir of ["desc", "asc"]) {
        await page.select("#sort-key", key);
        await page.click(`[data-sorter] .dir button[data-dir="${dir}"]`);
        const rows = await visible(page);
        const vals = rows.map((r) => (key === "msgs" ? r.m : r.v[keys.indexOf(key)]));
        const bad = monotone(vals, dir);
        if (bad) fail(`${city} ${key}-${dir}: порядок сломан на строке ${bad}: ${vals.slice(bad - 1, bad + 1)}`);
        const hash = await page.evaluate(() => location.hash);
        const wantHash = key === "msgs" && dir === "desc" ? "" : `#${key}-${dir}`;
        if (hash !== wantHash) fail(`${city} ${key}-${dir}: адрес ${hash}`);
        const note = await page.$eval("#sort-note", (n) => (n.hidden ? "" : n.textContent));
        if (key === "msgs") {
          if (rows.length !== total) fail(`${city}: по сообщениям видно ${rows.length} из ${total}`);
        } else {
          if (rows.some((r) => r.small)) fail(`${city} ${key}: видны ЖК меньше чем со 100 сообщениями`);
          const hidden = total - rows.length;
          if (hidden && !note.includes(`Ещё ${hidden} `)) fail(`${city} ${key}: в пояснении нет «${hidden}»: ${note}`);
          if (!rows[0].share || !/%$/.test(rows[0].share)) fail(`${city} ${key}: в колонке доли пусто`);
          const th = await page.$eval("#zhk-table th[data-share]", (t) => (t.hidden ? "" : t.textContent));
          if (th !== "Доля жалоб") fail(`${city} ${key}: заголовок колонки «${th}»`);
        }
      }
    }
    // Адрес с выбором восстанавливает сортировку при заходе по ссылке.
    await page.goto(`${base}/${city}/#voda-asc`, { waitUntil: "networkidle0" });
    await page.reload({ waitUntil: "networkidle0" });
    const st = await page.evaluate(() => ({ key: document.getElementById("sort-key").value,
      dir: document.querySelector('[data-sorter] .dir button[aria-pressed="true"]').dataset.dir }));
    const rows = await visible(page);
    const vi = (await page.$eval("#zhk-table", (t) => t.dataset.keys.split(","))).indexOf("voda");
    if (st.key !== "voda" || st.dir !== "asc" || monotone(rows.map((r) => r.v[vi]), "asc") || rows.some((r) => r.small)) {
      fail(`${city}: #voda-asc не восстановил выбор ${JSON.stringify(st)}`);
    }
    console.log(`  /${city}/: ${keys.length + 1} сортировок × 2 направления, монотонно; #voda-asc восстанавливается; ` +
                `при теме видно ${rows.length} из ${total}`);
  }

  // Главная: виджет.
  const data = JSON.parse(fs.readFileSync(path.join(OUT, "rating.json"), "utf8"));
  await page.goto(`${base}/`, { waitUntil: "networkidle0" });
  if (await page.$eval("[data-rating]", (b) => b.hidden)) fail("главная: виджет скрыт");
  let checked = 0;
  for (const c of [data.cities[0], data.cities[1], data.cities[data.cities.length - 1]]) {
    for (const key of ["all", "voda", "lift"]) {
      for (const dir of ["desc", "asc"]) {
        await page.select("#r-city", c.s);
        await page.select("#r-key", key);
        await page.click(`[data-rating] .dir button[data-dir="${dir}"]`);
        const got = await page.$$eval("#r-list li a", (as) => as.map((a) => a.getAttribute("href")));
        const k = data.keys.indexOf(key);
        const own = new Set(c.z.map((z) => z[1]));
        if (got.length !== Math.min(data.top, c.z.length)) fail(`главная ${c.s} ${key}: ${got.length} ЖК`);
        if (got.some((u) => !own.has(u))) fail(`главная ${c.s}: в списке ЖК другого города`);
        const vals = got.map((u) => c.z.find((z) => z[1] === u)[2][k]);
        if (monotone(vals, dir)) fail(`главная ${c.s} ${key}-${dir}: порядок ${vals}`);
        // Первый в списке — действительно крайний по городу.
        const ext = c.z.map((z) => z[2][k]);
        const want = dir === "asc" ? Math.min(...ext) : Math.max(...ext);
        if (vals[0] !== want) fail(`главная ${c.s} ${key}-${dir}: первый ${vals[0]}, а крайний ${want}`);
        const href = await page.$eval("#r-all", (a) => a.getAttribute("href"));
        if (href !== `/${c.s}/#${key}-${dir}`) fail(`главная: ссылка «все ЖК» ${href}`);
        checked++;
      }
    }
  }
  console.log(`  главная: ${checked} комбинаций город × тема × направление — 10 ЖК своего города по порядку, ссылка с сортировкой`);

  // Телефон: нет горизонтальной прокрутки страницы.
  await page.setViewport({ width: 390, height: 844, isMobile: true });
  for (const u of ["/", "/sankt-peterburg/#uk-desc", "/moskva/#all-asc"]) {
    await page.goto(base + u, { waitUntil: "networkidle0" });
    const w = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    if (w[0] > w[1]) fail(`${u} на 390 px шире экрана: ${w[0]} > ${w[1]}`);
    if (u !== "/") {
      const right = await page.$eval("#zhk-table tbody tr:not([hidden]) td[data-share]", (td) => td.getBoundingClientRect().right);
      if (right > w[1]) fail(`${u} на 390 px колонка доли за краем: ${Math.round(right)} > ${w[1]}`);
    }
  }
  console.log("  390 px: главная и две страницы городов без горизонтальной прокрутки");
  if (errors.length) fail("ошибки скрипта на странице: " + errors.join("; "));
  await browser.close();
  srv.close();
  console.log("SORT_UI_OK");
})().catch((e) => fail(e.stack || String(e)));
