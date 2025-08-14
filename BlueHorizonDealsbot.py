# -*- coding: utf-8 -*-
"""
BlueHorizonDeals — Telegram Game Deals Bot
Features:
- /sales    : Steam + Epic top discounts
- /freeepic : Epic free games this week
- /freesteam: Steam free games (scrape)
- /gtasales : GTA Online weekly offers (scrape Newswire)
- /addwishlist <AppID/URL/name>
- /showwishlist
- /removewishlist <AppID/URL/name>
- /trackprice <Steam AppID/URL>
- /untrackprice <Steam AppID/URL>
- /myalerts
- /upcomingsales
- Data persisted in SQLite (bot.db)
- Background job checks wishlist/tracking every CHECK_INTERVAL_MINUTES
"""

import os
import re
import sqlite3
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------------- CONFIG ----------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
STEAM_COUNTRY = os.getenv("STEAM_COUNTRY", "IN")
STEAM_LOCALE = os.getenv("STEAM_LOCALE", "en")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))

if not BOT_TOKEN:
    raise SystemExit("ERROR: BOT_TOKEN environment variable not set. Use .env or platform secrets.")

BOT_NAME = "BlueHorizonDeals"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (BlueHorizonDeals/1.0; +https://t.me/)",
    "Accept": "application/json, text/html;q=0.9"
}

DB_PATH = "bot.db"

# ---------------------- Database helpers ----------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # wishlist table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wishlist (
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            identifier TEXT NOT NULL,
            title TEXT,
            PRIMARY KEY(user_id, platform, identifier)
        )
    """)
    # tracking table (for price tracking)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracking (
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            identifier TEXT NOT NULL,
            title TEXT,
            threshold INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, platform, identifier)
        )
    """)
    # notify state to avoid repeated spam
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notify_state (
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            identifier TEXT NOT NULL,
            last_discount_percent INTEGER,
            last_price INTEGER,
            last_notified_at INTEGER,
            PRIMARY KEY(user_id, platform, identifier)
        )
    """)
    con.commit()
    con.close()

def db_add_wishlist(user_id, platform, identifier, title):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO wishlist(user_id, platform, identifier, title) VALUES (?, ?, ?, ?)",
                    (user_id, platform, identifier, title))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def db_get_wishlist(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT platform, identifier, title FROM wishlist WHERE user_id=?", (user_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def db_remove_wishlist(user_id, platform, identifier):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM wishlist WHERE user_id=? AND platform=? AND identifier=?", (user_id, platform, identifier))
    changed = cur.rowcount
    con.commit()
    con.close()
    return changed > 0

def db_add_tracking(user_id, platform, identifier, title, threshold=0):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO tracking(user_id, platform, identifier, title, threshold) VALUES (?, ?, ?, ?, ?)",
                    (user_id, platform, identifier, title, threshold))
        con.commit()
        return True
    except sqlite3.IntegrityError:
    > Manish:
return False
    finally:
        con.close()

def db_get_tracking(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT platform, identifier, title, threshold FROM tracking WHERE user_id=?", (user_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def db_remove_tracking(user_id, platform, identifier):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM tracking WHERE user_id=? AND platform=? AND identifier=?", (user_id, platform, identifier))
    changed = cur.rowcount
    con.commit()
    con.close()
    return changed > 0

def db_get_all_tracked():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id, platform, identifier, title, threshold FROM tracking")
    rows = cur.fetchall()
    con.close()
    return rows

def db_upsert_notify_state(user_id, platform, identifier, discount_percent, price):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    ts = int(time.time())
    cur.execute("""
        INSERT INTO notify_state(user_id, platform, identifier, last_discount_percent, last_price, last_notified_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, platform, identifier) DO UPDATE SET
            last_discount_percent=excluded.last_discount_percent,
            last_price=excluded.last_price,
            last_notified_at=excluded.last_notified_at
    """, (user_id, platform, identifier, discount_percent, price, ts))
    con.commit()
    con.close()

def db_get_notify_state(user_id, platform, identifier):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT last_discount_percent, last_price, last_notified_at FROM notify_state WHERE user_id=? AND platform=? AND identifier=?",
                (user_id, platform, identifier))
    row = cur.fetchone()
    con.close()
    return row

# ---------------------- Helpers: Steam/Epic/GTA ----------------------
def rupees(paise):
    if paise is None:
        return "—"
    try:
        return f"₹{int(paise)/100:,.0f}"
    except:
        return str(paise)

def fetch_steam_specials(limit=8):
    url = f"https://store.steampowered.com/api/featuredcategories?cc={STEAM_COUNTRY}&l={STEAM_LOCALE}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        items = data.get("specials", {}).get("items", [])[:limit]
        deals = []
        for it in items:
            deals.append({
                "name": it.get("name"),
                "appid": str(it.get("id")),
                "discount_percent": it.get("discount_percent", 0),
                "final_price": it.get("final_price"),
                "original_price": it.get("original_price"),
                "url": f"https://store.steampowered.com/app/{it.get('id')}"
            })
        return deals
    except Exception:
        return []

def fetch_steam_appdetails(appid):
    try:
        params = {"appids": appid, "cc": STEAM_COUNTRY, "l": STEAM_LOCALE}
        r = requests.get("https://store.steampowered.com/api/appdetails", params=params, headers=HEADERS, timeout=12)
        r.raise_for_status()
        j = r.json()
        if not j or not j.get(appid, {}).get("success"):
            return None
        data = j[appid].get("data", {})
        pov = data.get("price_overview")
        discount = pov.get("discount_percent") if pov else 0
        final = pov.get("final") if pov else None
        initial = pov.get("initial") if pov else None
        return {
            "name": data.get("name"),
            "discount_percent": int(discount or 0),
            "final_price": int(final) if final is not None else None,
            "original_price": int(initial) if initial is not None else None,
            "url": f"https://store.steampowered.com/app/{appid}"
        }
    except Exception:
        return None

def parse_steam_appid(text):
    text = text.strip()
    m = re.search(r"store\.steampowered\.com\/app\/(\d+)", text)
    if m:
        return m.group(1)
    if text.isdigit():
> Manish:
return text
    m2 = re.search(r"\((\d{3,7})\)$", text)
    if m2:
        return m2.group(1)
    return None

def fetch_epic_free_games():
    url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
        free_now = []
        for e in elements:
            title = e.get("title")
            promos = e.get("promotions") or {}
            promotionalOffers = promos.get("promotionalOffers") or []
            for offer_wrap in promotionalOffers:
                for offer in offer_wrap.get("promotionalOffers", []):
                    discount = offer.get("discountSetting", {}).get("discountPercentage")
                    if discount in (0, 100):
                        free_now.append({
                            "title": title,
                            "url": "https://store.epicgames.com/p/" + (e.get("productSlug") or "")
                        })
        return free_now
    except Exception:
        return []

def fetch_epic_top_discounts(limit=6):
    try:
        query = """
        query searchStoreQuery($allowCountries:String,$category:String,$country:String!,$locale:String,$sortBy:String,$onSale:Boolean){
          Catalog {
            searchStore(allowCountries:$allowCountries, category:$category, country:$country, locale:$locale, sortBy:$sortBy, onSale:$onSale) {
              elements { title productSlug price { totalPrice { discountPrice originalPrice } discount { discountPercentage } } }
            }
          }
        }
        """
        variables = {
            "allowCountries": "US",
            "category": "games/edition/base|bundles/games",
            "country": "US",
            "locale": "en-US",
            "sortBy": "discountPrice",
            "onSale": True
        }
        r = requests.post("https://store-site-backend-static.ak.epicgames.com/api/graphql",
                          json={"query": query, "variables": variables}, headers=HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])[:limit]
        deals = []
        for e in elements:
            title = e.get("title")
            slug = e.get("productSlug") or ""
            pr = e.get("price") or {}
            total = pr.get("totalPrice") or {}
            discount = (pr.get("discount") or {}).get("discountPercentage") or 0
            deals.append({
                "title": title,
                "discount_percent": int(discount),
                "original_price": total.get("originalPrice"),
                "final_price": total.get("discountPrice"),
                "url": f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/"
            })
        return deals
    except Exception:
        return []

def fetch_steam_free_games(limit=10):
    url = "https://store.steampowered.com/search/?maxprice=0&filter=globaltopsellers"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for row in soup.select(".search_result_row")[:limit]:
            title_tag = row.select_one(".title")
            title = title_tag.get_text(strip=True) if title_tag else None
            href = row.get("href")
            results.append({"title": title, "url": href})
        return results
    except Exception:
        return []

def fetch_gta_weekly():
    try:
        base = "https://www.rockstargames.com/newswire"
        r = requests.get(base, headers=HEADERS, timeout=12)
        r.raise_for_status()
        s = BeautifulSoup(r.text, "html.parser")
        link = None
        for a in s.find_all("a", href=True):
            t = a.get_text(" ", strip=True)
            href = a["href"]
> Manish:
if "GTA Online" in t and href.startswith("/newswire"):
                link = "https://www.rockstargames.com" + href
                break
        if not link:
            return {"title": "GTA News not found", "points": [], "url": base}
        r2 = requests.get(link, headers=HEADERS, timeout=12)
        r2.raise_for_status()
        s2 = BeautifulSoup(r2.text, "html.parser")
        title = s2.find(["h1","h2"]).get_text(strip=True) if s2.find(["h1","h2"]) else "GTA Online Update"
        points = []
        for li in s2.find_all(["li","p"]):
            txt = li.get_text(" ", strip=True)
            if txt and any(k in txt.lower() for k in ["% off", "discount", "x2", "double", "2x", "bonus", "rp", "discounted"]):
                points.append(txt)
        return {"title": title, "points": points[:12], "url": link}
    except Exception:
        return {"title": "Error fetching GTA news", "points": [], "url": ""}

def upcoming_events():
    return [
        {"platform": "Steam", "name": "Steam Seasonal Sales (Summer/Winter/Autumn) — dates vary"},
        {"platform": "Epic", "name": "Epic Mega Sale (annual) — dates vary"},
    ]

# ---------------------- Command handlers ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"नमस्ते! मैं *{BOT_NAME}* हूँ ߎ\n\n"
        "Commands के लिए /help टाइप करें।\n\n"
        "Best practice: Wishlist/Track के लिए Steam AppID या Steam Store URL दें — नाम भी चलेगा पर कम reliable है।"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "*Available Commands*\n\n"
        "/sales - Show Steam & Epic top discounts\n"
        "/freeepic - Epic free games this week\n"
        "/freesteam - Steam free games (search)\n"
        "/gtasales - GTA Online weekly offers\n"
        "/addwishlist <AppID/URL/name> - Add to wishlist\n"
        "/showwishlist - Show your wishlist\n"
        "/removewishlist <AppID/URL/name> - Remove from wishlist\n"
        "/trackprice <AppID/URL> - Start price tracking (Steam only)\n"
        "/untrackprice <AppID/URL> - Stop tracking\n"
        "/myalerts - Show active tracked games\n"
        "/upcomingsales - Upcoming major sales\n"
        "/about - About the bot"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = f"{BOT_NAME} — tracks game deals on Steam & Epic, shows free games and GTA weekly offers. Keep your token secret!"
    await update.message.reply_text(txt)

async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching deals... ⏳")
    steam = fetch_steam_specials(8)
    epic = fetch_epic_top_discounts(6)
    parts = []
    if steam:
        parts.append("*Steam Specials*")
        for s in steam:
            parts.append(f"• [{s['name']}]({s['url']}) — -{s['discount_percent']}%  {rupees(s['final_price'])}")
    else:
        parts.append("_No Steam deals found._")

    if epic:
        parts.append("\n*Epic Store Discounts*")
        for e in epic:
            op = e.get("original_price"); fp = e.get("final_price")
            price_str = f"${(fp or 0)/100:.2f} (MRP ${(op or 0)/100:.2f})" if fp else "—"
            parts.append(f"• [{e['title']}]({e['url']}) — -{e['discount_percent']}%  {price_str}")
    else:
        parts.append("\n_No Epic discounts found._")

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Steam Store", url="https://store.steampowered.com")]])
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

async def cmd_freeepic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching Epic free games... ⏳")
    free = fetch_epic_free_games()
    if not free:
        await update.message.reply_text("No free Epic games found right now.")
        return
> Manish:
lines = ["*Epic Free Games (Now)*"]
    for g in free:
        lines.append(f"• [{g['title']}]({g['url']})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_freesteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Searching Steam free games... ⏳")
    games = fetch_steam_free_games(limit=12)
    if not games:
        await update.message.reply_text("No Steam free games found (or scraping blocked).")
        return
    lines = ["*Steam Free Games (Search)*"]
    for g in games:
        lines.append(f"• [{g['title']}]({g['url']})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_gtasales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching GTA Online latest offers... ⏳")
    wk = fetch_gta_weekly()
    if not wk["points"]:
        await update.message.reply_text(f"*{wk['title']}*\n\n{wk['url']}", parse_mode=ParseMode.MARKDOWN)
        return
    msg = [f"*{wk['title']}*"]
    for p in wk["points"]:
        msg.append(f"• {p}")
    msg.append(f"\nSource: {wk['url']}")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

def normalize_identifier(text):
    if not text:
        return None, None
    text = text.strip()
    appid = parse_steam_appid(text)
    if appid:
        return "steam", appid
    # fallback: treat as name
    return "other", text

async def cmd_addwishlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /addwishlist <Steam AppID or Steam URL or game name>\nExample: /addwishlist 730")
        return
    raw = " ".join(context.args)
    platform, ident = normalize_identifier(raw)
    title = raw
    if platform == "steam":
        info = fetch_steam_appdetails(ident)
        if info:
            title = info.get("name") or title
    ok = db_add_wishlist(user.id, platform, ident, title)
    if ok:
        await update.message.reply_text(f"✅ Added to wishlist: *{title}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⚠️ Already in your wishlist.")

async def cmd_showwishlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = db_get_wishlist(user.id)
    if not rows:
        await update.message.reply_text("Your wishlist is empty. Add with /addwishlist <AppID/URL/name>")
        return
    lines = ["*Your Wishlist*"]
    for plat, ident, title in rows:
        if plat == "steam":
            info = fetch_steam_appdetails(ident)
            if info:
                lines.append(f"• [{info['name']}]({info['url']}) — -{info['discount_percent']}%  {rupees(info['final_price'])}")
            else:
                lines.append(f"• {title} (AppID {ident}) — details not found")
        else:
            lines.append(f"• {title}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_removewishlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /removewishlist <AppID/URL/name>")
        return
    raw = " ".join(context.args)
    plat, ident = normalize_identifier(raw)
    success = db_remove_wishlist(user.id, plat, ident)
    if success:
        await update.message.reply_text("✅ Removed from wishlist.")
    else:
        await update.message.reply_text("⚠️ Item not found in your wishlist.")

async def cmd_trackprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /trackprice <Steam AppID or Steam URL>\nExample: /trackprice 570")
        return
    raw = " ".join(context.args)
> Manish:
plat, ident = normalize_identifier(raw)
    if plat != "steam":
        await update.message.reply_text("Price tracking currently supports Steam AppID/URL only. Provide Steam AppID or URL.")
        return
    info = fetch_steam_appdetails(ident)
    title = info["name"] if info else raw
    ok = db_add_tracking(user.id, "steam", ident, title, threshold=0)
    if ok:
        await update.message.reply_text(f"ߔ Tracking started for *{title}* (AppID {ident}). You'll be notified on discounts.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⚠️ Already tracking this game.")

async def cmd_untrackprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /untrackprice <Steam AppID or Steam URL>")
        return
    raw = " ".join(context.args)
    plat, ident = normalize_identifier(raw)
    if plat != "steam":
        await update.message.reply_text("Provide Steam AppID/URL.")
        return
    success = db_remove_tracking(user.id, "steam", ident)
    if success:
        await update.message.reply_text("❌ Tracking stopped.")
    else:
        await update.message.reply_text("⚠️ Was not tracking that game.")

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = db_get_tracking(user.id)
    if not rows:
        await update.message.reply_text("You have no active price tracking alerts.")
        return
    lines = ["*Active Price Alerts*"]
    for plat, ident, title, threshold in rows:
        lines.append(f"• {title} (AppID {ident})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_upcomingsales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = upcoming_events()
    lines = ["*Upcoming Major Sales*"]
    for e in ev:
        lines.append(f"• {e['platform']}: {e['name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ---------------------- Background job to check tracked games & wishlist ----------------------
async def job_check_prices(context: ContextTypes.DEFAULT_TYPE):
    rows = db_get_all_tracked()
    for row in rows:
        user_id, plat, ident, title, threshold = row
        if plat != "steam":
            continue
        info = fetch_steam_appdetails(ident)
        if not info:
            continue
        disc = int(info.get("discount_percent", 0))
        final_price = info.get("final_price")
        state = db_get_notify_state(user_id, plat, ident)
        last_disc = state[0] if state else None
        last_price = state[1] if state else None
        notify = False
        reason = ""
        if disc > 0 and last_disc != disc:
            notify = True
            reason = f"Discount -{disc}%"
        elif final_price and last_price and final_price < last_price:
            notify = True
            reason = f"Price dropped from {rupees(last_price)} to {rupees(final_price)}"
        elif last_disc is None and disc > 0:
            notify = True
            reason = f"Discount -{disc}%"
        if notify:
            txt = (
                f"ߎ *Price Alert* — {title}\n"
                f"{reason}\n"
                f"{info['url']}\n"
                f"Now: {rupees(final_price)} (MRP {rupees(info.get('original_price'))})"
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=txt, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            except Exception:
                pass
            db_upsert_notify_state(user_id, plat, ident, disc, final_price)
        else:
            if state is None:
                db_upsert_notify_state(user_id, plat, ident, disc, final_price)

# ---------------------- Main ----------------------
def main():
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.
> Manish:
add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("freeepic", cmd_freeepic))
    app.add_handler(CommandHandler("freesteam", cmd_freesteam))
    app.add_handler(CommandHandler("gtasales", cmd_gtasales))
    app.add_handler(CommandHandler("addwishlist", cmd_addwishlist))
    app.add_handler(CommandHandler("showwishlist", cmd_showwishlist))
    app.add_handler(CommandHandler("removewishlist", cmd_removewishlist))
    app.add_handler(CommandHandler("trackprice", cmd_trackprice))
    app.add_handler(CommandHandler("untrackprice", cmd_untrackprice))
    app.add_handler(CommandHandler("myalerts", cmd_myalerts))
    app.add_handler(CommandHandler("upcomingsales", cmd_upcomingsales))

    # background job
    app.job_queue.run_repeating(job_check_prices, interval=CHECK_INTERVAL_MINUTES*60, first=30)

    print(f"✅ {BOT_NAME} is running (long polling)...")
    app.run_polling(close_loop=False)

if name == "__main__":
    main()

