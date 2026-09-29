// Сортировка ЖК по жалобам. Страница города: переставляет строки таблицы.
// Главная: виджет «город · тема · больше/меньше» по /rating.json.
// Доли — жалобы на тысячу сообщений чата; ЖК меньше чем со 100 сообщениями
// при выбранной теме не показываем: в маленьком чате пара сообщений меняет долю в разы.
(function () {
  function share(v) {
    var p = (v || 0) / 10;
    return p >= 10 ? Math.round(p) + "%" : p.toFixed(1).replace(".", ",") + "%";
  }
  function plural(n, f) {
    n = Math.abs(n) % 100;
    if (n >= 11 && n <= 19) return f[2];
    return n % 10 === 1 ? f[0] : (n % 10 >= 2 && n % 10 <= 4 ? f[1] : f[2]);
  }
  function setDir(box, dir) {
    box.querySelectorAll(".dir button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-dir") === dir));
    });
  }
  // Больше — сначала больше; при равенстве выше тот, у кого больше сообщений (цифра надёжнее).
  function cmp(dir) {
    return function (a, b) {
      if (a.v !== b.v) return dir === "asc" ? a.v - b.v : b.v - a.v;
      return b.m - a.m || a.i - b.i;
    };
  }

  // ── страница города ──
  var table = document.getElementById("zhk-table");
  var box = document.querySelector("[data-sorter]");
  if (table && box) {
    var keys = table.getAttribute("data-keys").split(",");
    var minMsgs = +table.getAttribute("data-min");
    var tbody = table.tBodies[0];
    var rows = Array.prototype.map.call(tbody.rows, function (tr, i) {
      return { tr: tr, i: i, m: +tr.getAttribute("data-m"), small: tr.hasAttribute("data-small"),
               vals: tr.getAttribute("data-v").split(",").map(Number), cell: tr.querySelector("td[data-share]") };
    });
    var sel = document.getElementById("sort-key");
    var note = document.getElementById("sort-note");
    var head = table.querySelector("th[data-share]");
    var state = { key: "msgs", dir: "desc" };

    var apply = function (push) {
      var byMsgs = state.key === "msgs";
      var k = keys.indexOf(state.key);
      rows.forEach(function (r) { r.v = byMsgs ? r.m : r.vals[k]; });
      var shown = rows.filter(function (r) { return byMsgs || !r.small; });
      var hidden = rows.length - shown.length;
      shown.sort(cmp(state.dir));
      rows.forEach(function (r) { r.tr.hidden = true; });
      shown.forEach(function (r) {
        r.tr.hidden = false;
        if (r.cell) { r.cell.textContent = share(r.v); r.cell.hidden = byMsgs; }
        tbody.appendChild(r.tr);
      });
      sel.value = state.key;
      head.hidden = byMsgs;
      table.classList.toggle("sorted", !byMsgs);
      if (hidden) {
        note.textContent = "Показаны " + shown.length + " ЖК, где за месяц " + minMsgs + " сообщений и больше. Ещё " +
          hidden + " " + plural(hidden, ["ЖК скрыт", "ЖК скрыто", "ЖК скрыто"]) +
          ": в маленьком чате пара сообщений меняет долю жалоб в разы.";
      }
      note.hidden = !hidden;
      setDir(box, state.dir);
      if (push) {
        var h = byMsgs && state.dir === "desc" ? "" : "#" + state.key + "-" + state.dir;
        history.replaceState(null, "", location.pathname + location.search + h);
      }
    };
    var fromHash = function () {
      var m = /^#([a-z]+)-(asc|desc)$/.exec(location.hash);
      if (m && (m[1] === "msgs" || keys.indexOf(m[1]) !== -1)) { state.key = m[1]; state.dir = m[2]; return true; }
      return false;
    };
    sel.addEventListener("change", function () { state.key = sel.value; apply(true); });
    box.querySelectorAll(".dir button").forEach(function (b) {
      b.addEventListener("click", function () { state.dir = b.getAttribute("data-dir"); apply(true); });
    });
    window.addEventListener("hashchange", function () { if (fromHash()) apply(false); });
    box.hidden = false;
    if (fromHash()) apply(false);
  }

  // ── главная ──
  var wbox = document.querySelector("[data-rating]");
  var list = document.getElementById("r-list");
  if (wbox && list) {
    var cSel = document.getElementById("r-city");
    var kSel = document.getElementById("r-key");
    var all = document.getElementById("r-all");
    var cName = document.getElementById("r-city-name");
    var data = null, dir = "desc";
    var render = function () {
      if (!data) return;
      var city = data.cities.filter(function (c) { return c.s === cSel.value; })[0];
      var k = data.keys.indexOf(kSel.value);
      var items = city.z.map(function (z, i) { return { n: z[0], u: z[1], v: z[2][k], m: z[3], i: i }; });
      items.sort(cmp(dir));
      list.innerHTML = "";
      items.slice(0, data.top).forEach(function (it) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = it.u;
        a.textContent = it.n;
        var s = document.createElement("span");
        s.className = "num";
        s.textContent = share(it.v);
        li.appendChild(a);
        li.appendChild(s);
        list.appendChild(li);
      });
      all.href = "/" + city.s + "/#" + kSel.value + "-" + dir;
      cName.textContent = city.c;
      setDir(wbox, dir);
    };
    cSel.addEventListener("change", render);
    kSel.addEventListener("change", render);
    wbox.querySelectorAll(".dir button").forEach(function (b) {
      b.addEventListener("click", function () { dir = b.getAttribute("data-dir"); render(); });
    });
    fetch("/rating.json").then(function (r) { return r.json(); }).then(function (d) {
      data = d;
      wbox.hidden = false;
      render();
    });
  }
})();
