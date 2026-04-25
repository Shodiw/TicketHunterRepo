"""
БЖД Монитор — серверная версия для Railway/Render
==================================================
Настройка через переменные окружения (Environment Variables):

  ROUTE_URL       — URL расписания с pass.rw.by (обязательно)
  TG_TOKEN        — токен Telegram бота (@BotFather → /newbot)
  TG_CHAT_ID      — ваш Chat ID (см. инструкцию ниже)
  CHECK_INTERVAL  — интервал проверки в секундах (по умолчанию: 120)
  LOWER_ONLY      — искать только нижние места: "1" или "0" (по умолчанию: 1)
  CAR_TYPES       — типы вагонов через запятую: "3,4" (3=плацкарт, 4=купе)
  TARGET_TRAINS   — номера поездов через запятую, пусто = все

Как узнать TG_CHAT_ID:
  1. @BotFather → /newbot → получите токен
  2. Напишите своему боту любое слово
  3. Откройте: https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Найдите "id" внутри "chat" — это ваш Chat ID
"""

import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bzd")

# ── Конфигурация из env ───────────────────────────────────────────────────────
def cfg():
    url = os.environ.get("ROUTE_URL", "").strip()
    if not url:
        log.error("ROUTE_URL не задан! Установите переменную окружения.")
        sys.exit(1)

    tg_token  = os.environ.get("TG_TOKEN",  "").strip()
    tg_chat   = os.environ.get("TG_CHAT_ID","").strip()
    interval  = int(os.environ.get("CHECK_INTERVAL", "120"))
    lower     = os.environ.get("LOWER_ONLY", "1").strip() == "1"
    raw_types = os.environ.get("CAR_TYPES", "3,4").strip()
    car_types = [int(x) for x in raw_types.split(",") if x.strip().isdigit()]
    raw_trains= os.environ.get("TARGET_TRAINS","").strip()
    trains    = [t.strip() for t in raw_trains.split(",") if t.strip()] or None

    return {
        "url":       url,
        "tg_token":  tg_token,
        "tg_chat":   tg_chat,
        "interval":  max(60, interval),
        "lower":     lower,
        "car_types": car_types or [3, 4],
        "trains":    trains,
    }

# ── Telegram ──────────────────────────────────────────────────────────────────
async def tg_send(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            d = await r.json()
            if d.get("ok"):
                log.info("Telegram: ✅ сообщение отправлено")
            else:
                log.warning(f"Telegram: ❌ {d.get('description','?')}")
    except Exception as e:
        log.warning(f"Telegram: ❌ {e}")

# ── Парсинг ───────────────────────────────────────────────────────────────────
LOWER_CODES = {"3Б", "3Д", "2К", "2Н", "2Б"}
CAR_NAMES   = {2: "Сидячий", 3: "Плацкарт", 4: "Купе", 5: "Мягкий", 6: "СВ"}

def _dec(s):
    return (s.replace("&quot;", '"').replace("&amp;", "&")
             .replace("&lt;", "<").replace("&gt;", ">"))

def _php(raw):
    try:
        import phpserialize
        r = phpserialize.loads(raw.encode(), decode_strings=True)
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}

def parse_route(rv, car_types, lower_only):
    r = {"train": "", "from": "", "to": "", "dep": "",
         "has_any": False, "has_lower": False, "details": []}
    p = _php(_dec(rv))
    if not p:
        return r
    r["train"] = p.get("train_number", "")
    r["from"]  = p.get("from_station_db", "")
    r["to"]    = p.get("to_station_db", "")
    ft = p.get("from_time", 0)
    if ft:
        try:
            r["dep"] = datetime.datetime.fromtimestamp(int(ft)).strftime("%d.%m %H:%M")
        except Exception:
            r["dep"] = str(ft)

    places = p.get("places", {})
    plist  = (list(places.values()) if isinstance(places, dict)
              else (places if isinstance(places, list) else []))
    for cg in plist:
        if not isinstance(cg, dict): continue
        ct = int(cg.get("car_type", 0))
        if car_types and ct not in car_types: continue
        pm  = cg.get("price_multi", {})
        pml = (list(pm.values()) if isinstance(pm, dict)
               else (pm if isinstance(pm, list) else []))
        for x in pml:
            if not isinstance(x, dict): continue
            cs = x.get("classservice", "")
            n  = int(x.get("places", 0))
            pr = x.get("prices", {})
            pv = (list(pr.values())[0] if isinstance(pr, dict) and pr
                  else (pr[0] if isinstance(pr, list) and pr else 0))
            if n > 0:
                r["has_any"] = True
                il = cs in LOWER_CODES
                if il: r["has_lower"] = True
                r["details"].append({"ct": ct, "cs": cs, "n": n,
                                     "price": pv, "lower": il})
    return r

# ── Одна проверка ─────────────────────────────────────────────────────────────
async def check_once(page, config):
    try:
        await page.goto(config["url"], timeout=60000, wait_until="networkidle")
        await asyncio.sleep(5)
        try:
            await page.wait_for_selector(
                ".js-sch-item-route,.sch-table__row,.no-results",
                timeout=15000,
            )
        except Exception:
            pass
        html = await page.content()
    except Exception as e:
        log.warning(f"Ошибка загрузки страницы: {e}")
        return []

    cnt = html.count('name="route"')
    if cnt == 0:
        if "captcha" in html.lower():
            log.warning("Обнаружена капча!")
        else:
            log.info("Расписание не загрузилось (0 поездов)")
        return []

    log.info(f"Поездов на странице: {cnt}")

    # Парсим route-инпуты
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        rvs  = [i.get("value", "") for i in
                soup.find_all("input", {"name": "route", "class": "js-sch-item-route"})]
    except ImportError:
        rvs = re.findall(r'name="route"\s+value="([^"]+)"', html)

    found = []
    for rv in rvs:
        info = parse_route(rv, config["car_types"], config["lower"])
        if not info["train"]:
            continue
        if config["trains"] and info["train"] not in config["trains"]:
            continue
        check = info["has_lower"] if config["lower"] else info["has_any"]
        if check:
            found.append(info)

    return found

# ── Формирование Telegram-сообщения ──────────────────────────────────────────
def build_tg_message(found_list, config):
    lines = ["🚂 <b>БЖД: ПОЯВИЛИСЬ БИЛЕТЫ!</b>\n"]
    for inf in found_list:
        lc = sum(d["n"] for d in inf["details"] if d["lower"])
        uc = sum(d["n"] for d in inf["details"] if not d["lower"])
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🚆 Поезд: <b>{inf['train']}</b>")
        lines.append(f"🗺 {inf['from']} → {inf['to']}")
        lines.append(f"🕐 Отправление: {inf['dep']}")
        if lc > 0:
            lines.append(f"🔽 Нижних мест: <b>{lc}</b>")
        if uc > 0:
            lines.append(f"🔼 Верхних мест: {uc}")
        lines.append("")
        # Детали по типам
        for d in inf["details"]:
            em  = "🔽" if d["lower"] else "🔼"
            lab = "Нижнее" if d["lower"] else "Верхнее"
            cn  = CAR_NAMES.get(d["ct"], f"Тип {d['ct']}")
            lines.append(f"  {em} {lab} · {cn} [{d['cs']}] · {d['n']} мест · {d['price']:.2f} BYN")

    lines.append("")
    lines.append(f"🔗 <a href=\"{config['url'][:200]}\">Открыть расписание</a>")
    lines.append(f"\n⏰ {datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    return "\n".join(lines)

# ── Главный цикл ──────────────────────────────────────────────────────────────
async def main():
    config = cfg()

    log.info("=" * 50)
    log.info("  БЖД Монитор билетов — серверная версия")
    log.info("=" * 50)
    log.info(f"  URL:       {config['url'][:70]}…")
    log.info(f"  Поезда:    {config['trains'] or 'все'}")
    log.info(f"  Вагоны:    {config['car_types']}")
    log.info(f"  Нижние:    {config['lower']}")
    log.info(f"  Интервал:  {config['interval']} сек")
    log.info(f"  Telegram:  {'✅ настроен' if config['tg_token'] else '❌ не настроен'}")
    log.info("=" * 50)

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--single-process",       # важно для Railway
            ],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="ru-RU",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": "https://pass.rw.by/ru/",
            },
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await ctx.new_page()

        # Тёплый старт
        log.info("Инициализация браузера…")
        await page.goto("https://pass.rw.by/ru/", timeout=30000,
                        wait_until="domcontentloaded")
        await asyncio.sleep(3)
        log.info("Браузер готов. Начинаем мониторинг.")

        # Отправляем стартовое сообщение в Telegram
        if config["tg_token"] and config["tg_chat"]:
            await tg_send(
                config["tg_token"], config["tg_chat"],
                f"🚂 <b>БЖД Монитор запущен</b>\n\n"
                f"Маршрут: {config['url'][config['url'].find('from='):config['url'].find('&from_exp')]}\n"
                f"Интервал проверки: {config['interval']} сек\n"
                f"Ищу: {'только нижние' if config['lower'] else 'любые'} места\n\n"
                f"Уведомлю как только появятся билеты 👀",
            )

        it = 0
        alerted = set()

        while True:
            it += 1
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"[{ts}] Проверка #{it}…")

            found = await check_once(page, config)
            new   = [f for f in found if f["train"] not in alerted]

            if new:
                for inf in new:
                    lc = sum(d["n"] for d in inf["details"] if d["lower"])
                    uc = sum(d["n"] for d in inf["details"] if not d["lower"])
                    log.info(f"  ✅ НАЙДЕНО! Поезд {inf['train']}: "
                             f"нижних={lc}, верхних={uc}")

                msg = build_tg_message(new, config)
                await tg_send(config["tg_token"], config["tg_chat"], msg)
                alerted.update(f["train"] for f in new)

            else:
                alerted &= {f["train"] for f in found}
                if found:
                    log.info(f"  ℹ Уже оповещено: {', '.join(f['train'] for f in found)}")
                else:
                    log.info("  ✗ Подходящих мест нет")

            log.info(f"  Следующая проверка через {config['interval']} сек…")
            await asyncio.sleep(config["interval"])


if __name__ == "__main__":
    asyncio.run(main())
