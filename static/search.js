// Поиск ЖК на главной: фильтр по заранее собранному списку, без сервера.
(function () {
  var input = document.getElementById("q");
  var out = document.getElementById("results");
  if (!input || !out) return;
  var items = [];
  fetch("/search.json").then(function (r) { return r.json(); }).then(function (d) { items = d; });
  function norm(s) { return (s || "").toLowerCase().replace(/ё/g, "е"); }
  input.addEventListener("input", function () {
    var q = norm(input.value.trim());
    out.innerHTML = "";
    if (q.length < 2) return;
    var hits = items.filter(function (it) { return norm(it.n).indexOf(q) !== -1; }).slice(0, 20);
    hits.forEach(function (it) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = it.u;
      a.textContent = "ЖК " + it.n + " ";
      var s = document.createElement("span");
      s.textContent = it.c;
      a.appendChild(s);
      li.appendChild(a);
      out.appendChild(li);
    });
    if (!hits.length) {
      var li = document.createElement("li");
      li.textContent = "Не нашли такой ЖК — возможно, в его чатах пока мало сообщений.";
      li.style.color = "#8a8a8a";
      out.appendChild(li);
    }
  });
})();
