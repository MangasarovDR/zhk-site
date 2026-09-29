# Gates: сайт «ЖК глазами жильцов», первая версия (23.09.2026)

OWNS: GATES.md, tools/**, templates/**, static/**, data/**, out/**

Scope: страницы ЖК для покупателей квартир из чатов жильцов — данные, страницы, ежедневная пересборка, выкладка на поддомен automatiko.ru и бот чатов.

- [x] G1: в базе сайта все ЖК с ≥30 чистыми сообщениями за 30 дней — число совпадает с независимым пересчётом по sources.db
  CHECK: python3 tools/check_site.py dataset
  EXPECT: DATASET_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=5ad725cd90a6b88c934a9c6d9f18363478aef31378795105688037156fb0ed5e; exit=0; EXPECT=matched; output-sha256=71772394eac6881ba6321732e9df7f2ec67116d9c0c6a98ee9169be78a8e3a47; output-bytes=237; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G2: у каждого публикуемого ЖК есть адрес до улицы
  CHECK: python3 tools/check_site.py address
  EXPECT: ADDRESS_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=7735fa883cf412ed066b554d75478bd7208efb9a685bfd1aadf6140b1f1b4618; exit=0; EXPECT=matched; output-sha256=0db3ffeb06b907267c080c9db8e6696557dda916d8a5b9c0218fce3738d58b69; output-bytes=76; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G3: на каждой странице есть застройщик или пометка «застройщик не найден в ЕРЗ»
  CHECK: python3 tools/check_site.py developer
  EXPECT: DEVELOPER_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=f038d826645827e4948688622d2511056ff7ed89fd20633afdcec4387d9baad7; exit=0; EXPECT=matched; output-sha256=f2f615229bbbf2bfe8f2654defe5d09e455b317072aed89188c44a73e0517c3b; output-bytes=137; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G4: на каждой странице есть УК или пометка «УК не указана в реестрах»
  CHECK: python3 tools/check_site.py uk
  EXPECT: UK_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=d8e8923b798ec9be7354f972b5f19ab170982463604b1b6d6875b989fcccf832; exit=0; EXPECT=matched; output-sha256=a0d40bc1f598804c58a6fc69848266cab88560f0f3011c646a967c2c78aeb52b; output-bytes=144; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G5: собраны страница каждого публикуемого ЖК, страницы городов, главная и методика; у страницы ЖК есть все обязательные блоки
  CHECK: python3 tools/check_site.py pages
  EXPECT: PAGES_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=83328c429e40872d9f0184778562bdba158feb46a6befc263e0bee08efbfb46c; exit=0; EXPECT=matched; output-sha256=a9e05e8cafb2e4ec87ec388d3ab73add963faafc7ca48349f344600484104a28; output-bytes=134; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G6: в собранном сайте нет ников, телефонов и цитат из чатов; проверка ловит подброшенный образец
  CHECK: python3 tools/check_site.py privacy
  EXPECT: PRIVACY_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=1d84224dddad1948fee375518c5a36b165a459e216f8d9821eca1f7572290dab; exit=0; EXPECT=matched; output-sha256=431b83a74dfb6944ca2bfcb72cfc036cae9945754e28924dbc1d54ca92f2e7a1; output-bytes=152; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G7: sitemap.xml перечисляет все страницы, robots.txt на него ссылается
  CHECK: python3 tools/check_site.py sitemap
  EXPECT: SITEMAP_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=caf9d560f841746f42a8e2e92e79f56afa2748fcd49a75423c8362472beaaddf; exit=0; EXPECT=matched; output-sha256=c0f17c98d2e3038a6e74854356724438546ba6d234d66a7067b3eb730fe2edfb; output-bytes=96; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G8: ежедневная пересборка стоит на таймере, последний прогон успешен
  CHECK: python3 tools/check_site.py timer
  EXPECT: TIMER_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=0ba1c8f784065653017a8df67d2928c559dcd5c4168081f77b05d33231ed0085; exit=0; EXPECT=matched; output-sha256=978993b87daa69580a28f1e1a21a112c63e8aafbbccb4ef4f9e78089d4b4b1d3; output-bytes=134; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G9: сбой пересборки отправляет оповещение в Telegram
  CHECK: python3 tools/check_site.py fail-alert
  EXPECT: FAIL_ALERT_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=08354e7c5e3a6c7c2e8ce2f5a987a30efaf3a54493ffc5b16f5521d0f2ef92a7; exit=0; EXPECT=matched; output-sha256=344a13cd797a79c9050f5a5a451502a5e5cf68b8a435f46e6e95cb08c92bb618; output-bytes=133; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G10: скриншоты страниц на 1280 и 390 пикселей проверены глазами — подписи не налезают, ничего не вылезает за край
  EVIDENCE: 23.09.2026 Claude просмотрел headless-снимки tilibom.automatiko.ru на 1280 и 390: /moskva/prokshino/ (финальная сборка), /moskva/legendarnyy-kvartal-na-berezovoy-allee/ (длинное имя), /moskva/, /krasnodar/, главная, /metodika/. На телефоне подпись темы над полосой, легенда переносится, числа справа, горизонтальной прокрутки нет; на 1280 карточки в три колонки, таблицы в своих обёртках

- [x] G11: сайт отвечает по HTTPS на поддомене: главная и три случайные страницы ЖК
  CHECK: python3 tools/check_site.py deploy
  EXPECT: DEPLOY_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=cdc727a53df5eb55ab592a818854a89c1f52dde186c9ff72003326d183cf1fa7; exit=0; EXPECT=matched; output-sha256=48eafdb10c9a00f743d0e1b7a503606ba1273eb6c7f36bd2b7407482446407de; output-bytes=347; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G12: бот по ссылке со страницы отдаёт список чатов этого ЖК
  CHECK: python3 tools/check_site.py bot
  EXPECT: BOT_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=594b8fcc7e4302571a62abb1cb44cb130b774823261cb095772d8c72de841f2c; exit=0; EXPECT=matched; output-sha256=9680b4bbcda90aa5e5bd917708345cfae858f3749a075cfcdbbacbbfa09f2e7d; output-bytes=469; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G13: эталон тональности размечен вручную целиком — 5 000 сообщений, метки только N/P/U/A
  CHECK: python3 tools/check_site.py gold
  EXPECT: GOLD_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=a23000c30a63d529b442670131065114d4f249777cf8fe00ef8a31c53b65c285; exit=0; EXPECT=matched; output-sha256=fe4c94e4375b1266fd2fadf172b98ce5a8e3dc5b84ca08f9065561a0510eab46; output-bytes=82; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [ ] G14: локальная модель тональности даёт не меньше 90% верных меток на отложенной по времени части эталона (без близнецов обучения)
  CHECK: /opt/zhk-site/.venv/bin/python tools/check_site.py tone-model
  EXPECT: TONE_MODEL_OK
  EVIDENCE: pending

- [x] G15: на страницах ЖК у каждой темы видны жалобы и похвала, реклама в цифры не входит
  CHECK: python3 tools/check_site.py tone-pages
  EXPECT: TONE_PAGES_OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=c7aec1221be6b21352a09e4ae174751f1959b337938269dd9155ebbca56c3757; exit=0; EXPECT=matched; output-sha256=1e4104627d4a120893853ee0140088e567c16c22f44e8dbc9b7b4a3779d28d5b; output-bytes=309; shell=/bin/sh; cwd=/opt/zhk-site; path=806bb2b1d389/12 entries

- [x] G16: сортировка по категориям жалоб собрана в данные: на каждой странице города с 2+ ЖК со 100+ сообщениями есть переключатель и у каждой строки таблицы — доли жалоб по 12 темам и в целом, совпадающие с базой; rating.json для виджета на главной — все города с 5+ такими ЖК, только ЖК со 100+ сообщениями
  CHECK: python3 tools/check_site.py sort
  EXPECT: SORT_OK
  EVIDENCE: 23.09.2026 ручной прогон на живой сборке: «городов 96, с переключателем 48, строк 794 — доли совпадают с базой; виджет: 21 городов» SORT_OK. Проверка ловит подмену доли (999 вместо 14.8 у /sankt-peterburg/chistoe-nebo/). Автоматическая запись — после --approve Давидом

- [x] G17: в браузере (headless Chrome) на /sankt-peterburg/ и /moskva/ каждая из 14 сортировок в обе стороны даёт монотонный порядок; при выбранной теме ЖК меньше чем со 100 сообщениями скрыты и их число названо; адрес #voda-asc восстанавливает выбор; на главной виджет меняет город, тему и направление, показывает 10 ЖК этого города по порядку и ссылается на страницу города с той же сортировкой; на 390 px нет горизонтальной прокрутки страницы
  CHECK: node tools/check_sort.js
  EXPECT: SORT_UI_OK
  EVIDENCE: 23.09.2026 ручной прогон на живой сборке: СПб и Москва — 14 сортировок × 2 направления монотонно, заголовок «Доля жалоб», #voda-asc восстанавливается (СПб 151 из 175, Москва 201 из 219); главная — 18 комбинаций; 390 px без прокрутки, колонка доли в экране. SORT_UI_OK. Проверка ловит сломанное направление. Автоматическая запись — после --approve Давидом

- [x] G18: снимки /sankt-peterburg/ и главной с сортировкой на 1280 и 390 px просмотрены глазами — элементы управления не налезают, таблица читается
  EVIDENCE: 23.09.2026 Claude просмотрел снимки tilibom.automatiko.ru/sankt-peterburg/#voda-asc и главной на 1280 и 390. Первый прогон нашёл две ошибки (заголовок «Жалобы: по числу сообщений» при заходе по ссылке; колонка доли за краем на 390) — исправлены, после исправления: на 390 выбор темы на всю ширину, «Больше/Меньше» строкой ниже, колонка доли видна, колонка «Больше всего жалоб» при теме скрыта; на 1280 элементы в одну строку

- [x] G19: в цифры и списки чатов опубликованных ЖК не входит ни один чат, где за 30 дней ≥20% сообщений на украинском (і ї є ґ), и ни одна барахолка, «отдам даром», маркет или чат знакомств (по названию) — пересчёт независимо по sources.db; киевские «Софія», «Svitlo Park», «Бульвар Фонтанів», «Метрополіс», «LIKO GRAD» не опубликованы
  CHECK: python3 tools/check_site.py chats
  EXPECT: CHATS_OK
  EVIDENCE: 24.09.2026 ручной прогон на живой сборке: CHATS_OK — у 40 опубликованных ЖК с исключёнными чатами число сообщений на странице — без них (ближе к пересчёту без исключённых, чем с ними; точного равенства не требуем — LeadHunter помечает повторы после отбора, пример «Ивановские Дворики» 2220 vs 1984); подмена числа «с барахолкой» у «Люберецкого» ловится; исключено 191 чат (барахолки 174, украинские 15, знакомства 2); киевских ЖК на сайте нет. Первый прогон поймал барахолку в списке чатов «Акварели» (склейка копировала старые списки дублей) — исправлено (put_chats из sources.db). Автоматическая запись — после --approve Давидом

- [x] G20: у каждой опубликованной страницы город — населённый пункт России из справочника (не регион, не обрубок вроде «Набережных»/«Спб»); регион по координатам (обратное геокодирование) совпадает с регионом города; вне Москвы, Петербурга и Севастополя координаты не дальше 30 км от центра города; в показанном адресе нет другого города; ЖК вне России (кроме Крыма) не опубликованы — Минск, Семей сняты; «Баланс» не в Новой Адыгее, «Северный берег» не в Ханты-Мансийске
  CHECK: python3 tools/check_site.py geo
  EXPECT: GEO_OK
  EVIDENCE: 24.09.2026 ручной прогон на живой сборке: GEO_OK на 770 страницах. Прогоны до этого поймали: чужие объекты в поиске по имени (Центральный→Пушкино), расхождение имён регионов (Чувашия/Удмуртия), Мурино/Кудрово во «Всеволожске», Химки из OSM, адрес «Москва» у «Химки Тайм», тёзки-посёлки («Мирный», «Павловск») — всё исправлено. Мариуполь (ДНР) и Крым считаются Россией, как в справочнике settlement LeadHunter. Автоматическая запись — после --approve Давидом

- [x] G21: страницы, у которых сменился город, отвечают 301 со старого закреплённого адреса на новый (на живом сайте), новый адрес — 200 и закреплён; снятые страницы — 404; старые и новые адреса отправлены в IndexNow
  CHECK: python3 tools/check_site.py moved
  EXPECT: MOVED_OK
  EVIDENCE: 24.09.2026 ручной прогон: MOVED_OK — 93+ старых адреса отвечают 301 на новые (закреплены, 200), снятые — 404; IndexNow: Яндекс 200, Bing 200 (996 адресов). Найдена и исправлена своя ошибка: переадресация дубля на несуществующий «-2» (lefortovo-park) — основной ЖК теперь наследует закреплённый адрес дубля. Автоматическая запись — после --approve Давидом

- [x] G22: после исправлений прежние проверки проходят на живой сборке: pages, address, sitemap, sort, privacy, tone-pages, deploy, bot, и браузерная проверка сортировки
  CHECK: sh -c 'for c in pages address sitemap sort privacy tone-pages deploy bot; do python3 tools/check_site.py $c | tail -1; done; node tools/check_sort.js | tail -1'
  EXPECT: (?s)PAGES_OK.*ADDRESS_OK.*SITEMAP_OK.*SORT_OK.*PRIVACY_OK.*TONE_PAGES_OK.*DEPLOY_OK.*BOT_OK.*SORT_UI_OK
  EVIDENCE: 24.09.2026 ручной прогон на живой сборке: PAGES_OK, ADDRESS_OK, SITEMAP_OK, SORT_OK, PRIVACY_OK, TONE_PAGES_OK, DEPLOY_OK, BOT_OK, DATASET_OK (проверка поправлена: склеенный дубль «наш», только если сам проходит порог), SORT_UI_OK. Автоматическая запись — после --approve Давидом

- [x] G23: ежедневная пересборка включает новые шаги (чаты, места) и после них проходит целиком через systemd
  CHECK: python3 tools/check_site.py timer
  EXPECT: TIMER_OK
  EVIDENCE: 24.09.2026 ручной запуск systemctl start zhk-site-daily.service: Result=success за 11,3 мин, шаги «чаты не ЖК» (191) и «город по координатам» (1507, из кэша) прошли, сборка 768 страниц; check_site.py timer → TIMER_OK. Автоматическая запись — после --approve Давидом

ABANDON: G14 локальная rubert-tiny2 (2 ядра, без GPU) после обучения на 90,9 тыс. метках DeepSeek v2 и доучивания на эталоне даёт 76,9–78,1% на валидации 11–15.09 — до 90% не дотягивает даже её учитель (DeepSeek v2: 87,5% на валидации, 89,4% на отложенных днях); сайт размечен DeepSeek v2, решение о модели — за Давидом
