#!/usr/bin/env python3
"""
Telegram bot that monitors trouverunlogement.lescrous.fr for newly published
student housing offers ("logements") in a chosen search zone.

MULTI-USER VERSION WITH WEBHOOK
----------------------------------
Every chat_id gets its own independent state (zone, known ids, monitoring
flag, etc.) and its own background "worker" task. Starting /monitor in one
person's chat has zero effect on anyone else's.

Deployment: Uses webhook mode for Render Web Service (free tier).
"""

import asyncio
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from functools import wraps
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, request as flask_request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(dotenv_path=".env", override=False)

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")

ALLOWED_CHAT_IDS = {
    c.strip() for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}

NTFY_TOPIC_BASE = os.getenv("NTFY_TOPIC", "")

CROUS_SEARCH_BASE_URL = os.getenv(
    "CROUS_SEARCH_BASE_URL",
    "https://trouverunlogement.lescrous.fr/tools/47/search",
)
CROUS_MAP_URL = CROUS_SEARCH_BASE_URL

GEO_API_BASE = "https://geo.api.gouv.fr"

CHECK_INTERVAL       = int(os.getenv("CHECK_INTERVAL", "5"))
REQUEST_TIMEOUT       = 30
BACKOFF_AFTER_BLOCK    = 300
BACKOFF_AFTER_ERROR    = 10
MAX_ERROR_STREAK       = 5
BBOX_PADDING_DEG       = 0.01

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "localhost:5000")
WEBHOOK_PATH = "/telegram"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Rotating user-agent pool ──────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

# ── Per-user state ─────────────────────────────────────────────────────────────
USER_STATES: Dict[int, dict] = {}
MONITOR_TASKS: Dict[int, asyncio.Task] = {}
USER_SESSIONS: Dict[int, requests.Session] = {}

# Global Application instance (set by main)
telegram_app: Optional[Application] = None


def get_state(chat_id: int) -> dict:
    """Return (creating if needed) the per-user state dict for this chat_id."""
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = {
            "monitoring":   False,
            "search_url":   None,
            "zone_label":   None,
            "known_ids":    set(),
            "baseline_recorded": False,
            "blocked":      False,
            "check_count":  0,
            "last_check":   None,
            "extra_wait":   0,
            "error_streak": 0,
            "awaiting":     None,
            "last_404_time": 0,
        }
    return USER_STATES[chat_id]


def get_session(chat_id: int) -> requests.Session:
    """Get or create a requests.Session for this user (connection reuse)."""
    if chat_id not in USER_SESSIONS:
        session = requests.Session()
        session.headers.update({
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":           "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding":           "gzip, deflate, br",
            "Connection":                "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control":             "no-cache",
            "Referer":                   CROUS_SEARCH_BASE_URL,
        })
        USER_SESSIONS[chat_id] = session
    return USER_SESSIONS[chat_id]


# ── Access control ────────────────────────────────────────────────────────────

def restricted(handler):
    """
    If ALLOWED_CHAT_IDS is empty, everyone is allowed (public multi-user bot).
    If it's non-empty, only those chat ids can use the bot.
    """

    @wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if ALLOWED_CHAT_IDS and str(chat_id) not in ALLOWED_CHAT_IDS:
            logger.warning(f"Ignored message from unauthorized chat {chat_id}")
            return
        return await handler(update, ctx)

    return wrapper


# ── Geocoding ─────────────────────────────────────────────────────────────────

def _bbox_from_geojson(geometry: dict) -> Optional[tuple]:
    """Return (min_lon, min_lat, max_lon, max_lat) from a GeoJSON Polygon/MultiPolygon."""
    if not geometry:
        return None

    def walk(coords):
        if isinstance(coords[0], (float, int)):
            yield coords
        else:
            for c in coords:
                yield from walk(c)

    lons, lats = [], []
    for lon, lat in walk(geometry["coordinates"]):
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


def geocode_place(query: str) -> Optional[dict]:
    """
    Resolve a city name, city+postcode, or département name/code to a bounding box.
    """
    query = query.strip()

    if re.fullmatch(r"\d{2,3}|2[AB]", query.upper()):
        try:
            r = requests.get(
                f"{GEO_API_BASE}/departements/{query.upper()}",
                params={"fields": "nom,code,contour"},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                bbox = _bbox_from_geojson(data.get("contour"))
                if bbox:
                    return {"label": f"{data['nom']} ({data['code']})", "bounds": bbox}
        except requests.exceptions.RequestException as exc:
            logger.error(f"Geocoding (département) failed: {exc}")

    try:
        r = requests.get(
            f"{GEO_API_BASE}/communes",
            params={
                "nom": query,
                "fields": "nom,code,centre,contour,departement",
                "boost": "population",
                "limit": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            commune = results[0]
            bbox = _bbox_from_geojson(commune.get("contour"))
            if not bbox and commune.get("centre"):
                lon, lat = commune["centre"]["coordinates"]
                bbox = (lon, lat, lon, lat)
            if bbox:
                dept = commune.get("departement", {}).get("nom", "")
                label = f"{commune['nom']} ({dept})" if dept else commune["nom"]
                return {"label": label, "bounds": bbox}
    except requests.exceptions.RequestException as exc:
        logger.error(f"Geocoding (commune) failed: {exc}")

    try:
        r = requests.get(
            f"{GEO_API_BASE}/departements",
            params={"nom": query, "fields": "nom,code,contour", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            dept = results[0]
            bbox = _bbox_from_geojson(dept.get("contour"))
            if bbox:
                return {"label": f"{dept['nom']} ({dept['code']})", "bounds": bbox}
    except requests.exceptions.RequestException as exc:
        logger.error(f"Geocoding (département name) failed: {exc}")

    return None


def build_search_url(bounds: tuple) -> str:
    min_lon, min_lat, max_lon, max_lat = bounds
    min_lon -= BBOX_PADDING_DEG
    max_lon += BBOX_PADDING_DEG
    min_lat -= BBOX_PADDING_DEG
    max_lat += BBOX_PADDING_DEG
    bounds_str = f"{min_lon:.7f}_{max_lat:.7f}_{max_lon:.7f}_{min_lat:.7f}"
    sep = "&" if "?" in CROUS_SEARCH_BASE_URL else "?"
    return f"{CROUS_SEARCH_BASE_URL}{sep}bounds={bounds_str}"


# ── Website checker ───────────────────────────────────────────────────────────

def check_listings(search_url: str, chat_id: int) -> dict:
    """
    Fetch the CROUS search page and return a dict with status, ids, and listings.
    """
    session = get_session(chat_id)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)

    try:
        resp = session.get(search_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {
            "status": "error",
            "detail": f"Request timed out after {REQUEST_TIMEOUT}s (site might be slow)",
            "http_code": None,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }
    except requests.exceptions.ConnectionError as exc:
        return {
            "status": "error",
            "detail": f"Connection error: {exc}",
            "http_code": None,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }
    except requests.exceptions.RequestException as exc:
        return {
            "status": "error",
            "detail": f"Request failed: {exc}",
            "http_code": None,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }

    code = resp.status_code

    if code == 429:
        retry_after = resp.headers.get("Retry-After", "unknown")
        return {
            "status": "rate_limited",
            "detail": f"HTTP 429 – Rate limited. Retry-After: {retry_after}s",
            "http_code": 429,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }
    if code == 403:
        return {
            "status": "blocked",
            "detail": "HTTP 403 Forbidden (IP blocked or session invalid)",
            "http_code": 403,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }
    if code == 503:
        return {
            "status": "error",
            "detail": "HTTP 503 Service Unavailable (CROUS maintenance?)",
            "http_code": 503,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }
    if code not in (200, 304):
        return {
            "status": "error",
            "detail": f"Unexpected HTTP {code}",
            "http_code": code,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }

    page_text_lower = resp.text.lower()
    captcha_signals = ["captcha", "i'm not a robot", "je ne suis pas un robot", "cloudflare", "trop nombreux", "recaptcha"]
    if any(sig in page_text_lower for sig in captcha_signals):
        return {
            "status": "captcha",
            "detail": "CAPTCHA / anti-bot page detected. Backing off.",
            "http_code": 200,
            "ids": set(),
            "listings": {},
            "total_text": None,
        }

    soup = BeautifulSoup(resp.text, "html.parser")

    ids: set = set()
    listings: dict = {}
    
    for a in soup.find_all("a", href=True):
        m = re.search(r"/accommodations/(\d+)", a["href"])
        if not m:
            continue
        aid = m.group(1)
        ids.add(aid)
        if aid in listings:
            continue
        
        title = a.get_text(strip=True) or f"Logement #{aid}"
        
        container = a
        for _ in range(5):
            if container.parent is not None:
                container = container.parent
            else:
                break
        
        context_text = container.get_text(" ", strip=True)
        
        price_match = re.search(r"(\d+(?:\s?\d{3})*(?:,\d+)?)\s*€", context_text)
        price = price_match.group(0).strip() if price_match else "prix non précisé"
        
        full_url = a["href"]
        if not full_url.startswith("http"):
            full_url = f"https://trouverunlogement.lescrous.fr{full_url}"
        
        listings[aid] = {"title": title, "price": price, "url": full_url}

    total_match = re.search(r"(\d+)\s*logements?\s*trouv[ée]s?", resp.text, re.IGNORECASE)
    total_text = total_match.group(0) if total_match else None

    if len(ids) == 0:
        if len(resp.text) < 5000:
            return {
                "status": "empty_page",
                "detail": f"Page looks empty or didn't render (possible JS loading issue)",
                "http_code": 200,
                "ids": set(),
                "listings": {},
                "total_text": total_text,
            }

    return {
        "status":     "ok",
        "detail":     total_text or f"{len(ids)} logement(s) trouvé(s) sur la page",
        "http_code":  200,
        "ids":        ids,
        "listings":   listings,
        "total_text": total_text,
    }


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_notification(chat_id: int, text: str, reply_markup=None) -> None:
    if not telegram_app:
        return
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except Exception as exc:
        logger.error(f"Failed to send Telegram message to {chat_id}: {exc}")


async def send_alarm(chat_id: int, text: str) -> None:
    """Loud notification for a brand-new listing."""
    if not telegram_app:
        return
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text="🚨🔥 " + text,
            parse_mode=ParseMode.HTML,
            disable_notification=False,
        )
    except Exception as exc:
        logger.error(f"Failed to send alarm message to {chat_id}: {exc}")


def send_ntfy_alarm(chat_id: int, listing_title: str) -> None:
    """Send a max-priority push notification via ntfy.sh, on a per-user sub-topic."""
    if not NTFY_TOPIC_BASE:
        return
    topic = f"{NTFY_TOPIC_BASE}-{chat_id}"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            headers={
                "Title":    "LOGEMENT CROUS DISPONIBLE !!!",
                "Priority": "urgent",
                "Tags":     "rotating_light,house",
            },
            data=f"Nouveau logement : {listing_title}".encode("utf-8"),
            timeout=10,
        )
        logger.info(f"ntfy alarm sent on topic {topic}")
    except Exception as exc:
        logger.error(f"Failed to send ntfy notification: {exc}")


# ── Monitoring loop ───────────────────────────────────────────────────────────

async def monitor_loop(chat_id: int) -> None:
    state = get_state(chat_id)
    logger.info(f"Monitoring loop started for chat {chat_id}")
    await send_notification(
        chat_id,
        f"🔍 <b>Surveillance démarrée</b>\n"
        f"Zone : {state['zone_label'] or 'personnalisée'}\n"
        f"Fréquence : toutes les {CHECK_INTERVAL}s\n"
        f"🔗 {state['search_url']}"
    )

    try:
        while state["monitoring"]:
            if state["extra_wait"] > 0:
                wait = state["extra_wait"]
                state["extra_wait"] = 0
                logger.info(f"[{chat_id}] Back-off: waiting {wait}s before next check")
                for _ in range(wait):
                    if not state["monitoring"]:
                        break
                    await asyncio.sleep(1)
                if not state["monitoring"]:
                    break

            result = check_listings(state["search_url"], chat_id)
            status = result["status"]
            detail = result["detail"]
            prev_blocked = state["blocked"]

            state["check_count"] += 1
            state["last_check"] = datetime.now()
            logger.info(f"[{chat_id}] Check #{state['check_count']}: [{status}] {detail}")

            if status in ("blocked", "rate_limited", "captcha"):
                state["error_streak"] = 0
                if not state["blocked"]:
                    state["blocked"] = True
                    await send_notification(
                        chat_id,
                        f"⛔ <b>Surveillance temporairement bloquée !</b>\n"
                        f"Raison : {detail}\n\n"
                        f"Pause de {BACKOFF_AFTER_BLOCK // 60} minutes avant la prochaine tentative…"
                    )
                state["extra_wait"] = BACKOFF_AFTER_BLOCK
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if prev_blocked and status == "ok":
                state["blocked"] = False
                await send_notification(
                    chat_id,
                    "✅ <b>Surveillance reprise</b> – les requêtes fonctionnent à nouveau."
                )

            if status == "empty_page":
                state["error_streak"] += 1
                current_time = time.time()
                if current_time - state["last_404_time"] > 60:
                    await send_notification(
                        chat_id,
                        f"⚠️ <b>Page vide ou ne s'affiche pas correctement</b>\n"
                        f"{detail}\n"
                        f"Le site utilise peut-être JavaScript. Nouvelle tentative dans {BACKOFF_AFTER_ERROR}s."
                    )
                    state["last_404_time"] = current_time
                state["extra_wait"] = BACKOFF_AFTER_ERROR
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if status == "error":
                state["error_streak"] += 1

                if result.get("http_code") == 500:
                    state["extra_wait"] = BACKOFF_AFTER_ERROR
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                if state["error_streak"] == 1:
                    await send_notification(
                        chat_id,
                        f"⚠️ <b>Échec de la vérification</b>\n"
                        f"{detail}\n"
                        f"Nouvelle tentative dans {BACKOFF_AFTER_ERROR}s."
                    )
                
                if state["error_streak"] >= MAX_ERROR_STREAK:
                    await send_notification(
                        chat_id,
                        f"❌ <b>Trop d'erreurs consécutives ({state['error_streak']})</b>\n"
                        f"Pause de 2 minutes pour laisser le serveur respirer."
                    )
                    state["extra_wait"] = 120
                else:
                    state["extra_wait"] = BACKOFF_AFTER_ERROR

                await asyncio.sleep(CHECK_INTERVAL)
                continue

            state["error_streak"] = 0

            current_ids = result["ids"]

            if not state["baseline_recorded"]:
                state["known_ids"] = current_ids
                state["baseline_recorded"] = True
                await send_notification(
                    chat_id,
                    f"ℹ️ Référence enregistrée : <b>{len(current_ids)}</b> logement(s) actuellement visibles "
                    f"dans cette zone.\nJe vous alerte dès qu'un nouveau logement apparaît."
                )
            else:
                new_ids = current_ids - state["known_ids"]

                if new_ids:
                    logger.info(f"[{chat_id}] Found {len(new_ids)} NEW listings: {new_ids}")
                    for aid in sorted(new_ids):
                        info = result["listings"].get(aid, {})
                        title = info.get("title", f"Logement #{aid}")
                        price = info.get("price", "prix non précisé")
                        url = info.get("url", state["search_url"])

                        send_ntfy_alarm(chat_id, title)

                        await send_alarm(
                            chat_id,
                            f"<b>NOUVEAU LOGEMENT CROUS !</b>\n"
                            f"🏠 {title}\n"
                            f"💶 {price}\n"
                            f"👉 <a href=\"{url}\">Voir l'annonce et réserver</a>"
                        )
                else:
                    logger.info(f"[{chat_id}] No new listings (current: {len(current_ids)}, known: {len(state['known_ids'])})")

                state["known_ids"] = current_ids

            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        state["monitoring"] = False
        MONITOR_TASKS.pop(chat_id, None)
        logger.info(f"Monitoring loop stopped for chat {chat_id}")


# ── Command handlers ──────────────────────────────────────────────────────────

MAIN_MENU_TEXT = (
    "🏠 <b>Crous Logement Watch Bot</b>\n\n"
    "<b>Choisir une zone de recherche :</b>\n"
    "/ville – taper le nom d'une ville ou d'un département\n"
    "/carte – choisir une zone sur la carte interactive du site\n"
    "/zone – voir la zone actuellement configurée\n\n"
    "<b>Surveillance :</b>\n"
    "/monitor – démarrer la surveillance\n"
    "/stop – arrêter la surveillance\n"
    "/check – vérifier immédiatement\n"
    "/status – voir l'état du bot\n"
    "/test – déclencher une fausse alerte (test)\n"
    "/reset – effacer la zone choisie\n"
    "/help – afficher ce message"
)


@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    get_state(chat_id)
    await update.message.reply_html(
        f"👋 Bienvenue !\n\nVotre chat ID est : <code>{chat_id}</code>\n\n{MAIN_MENU_TEXT}"
    )


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(MAIN_MENU_TEXT)


@restricted
async def cmd_ville(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state["awaiting"] = "city"
    await update.message.reply_html(
        "🏙️ Tapez le nom d'une <b>ville</b> (ex : <i>Lyon</i>) ou d'un <b>département</b> "
        "(ex : <i>Gironde</i> ou <i>33</i>) dans votre prochain message."
    )


@restricted
async def cmd_carte(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state["awaiting"] = "url"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗺️ Cliquer ici pour choisir la zone sur la carte", url=CROUS_MAP_URL)]]
    )
    await update.message.reply_html(
        "1️⃣ Cliquez sur le bouton ci-dessous.\n"
        "2️⃣ Sur le site, cliquez sur <b>« Afficher sur une carte »</b>, déplacez-vous "
        "jusqu'à la zone qui vous intéresse, puis cliquez sur <b>« Rechercher dans la zone »</b>.\n"
        "3️⃣ Copiez l'URL affichée dans la barre d'adresse (elle contient <code>bounds=</code>) "
        "et collez-la moi ici, dans votre prochain message.",
        reply_markup=keyboard,
    )


@restricted
async def cmd_zone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not state["search_url"]:
        await update.message.reply_text("Aucune zone configurée pour l'instant. Utilisez /ville ou /carte.")
        return
    await update.message.reply_html(
        f"📍 Zone actuelle : <b>{state['zone_label'] or 'personnalisée'}</b>\n🔗 {state['search_url']}"
    )


@restricted
async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state["search_url"] = None
    state["zone_label"] = None
    state["known_ids"] = set()
    state["awaiting"] = None
    await update.message.reply_text("🗑️ Zone effacée. Utilisez /ville ou /carte pour en choisir une nouvelle.")


@restricted
async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state["search_url"]:
        await update.message.reply_text("⚠️ Choisissez d'abord une zone avec /ville ou /carte.")
        return
    if state["monitoring"] or chat_id in MONITOR_TASKS:
        await update.message.reply_text("Surveillance déjà active ! Utilisez /status pour voir l'état.")
        return
    state["monitoring"]        = True
    state["blocked"]           = False
    state["extra_wait"]        = 0
    state["error_streak"]      = 0
    state["baseline_recorded"] = False
    state["known_ids"]         = set()
    MONITOR_TASKS[chat_id] = asyncio.create_task(monitor_loop(chat_id))


@restricted
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state["monitoring"]:
        await update.message.reply_text("La surveillance n'est pas active.")
        return
    state["monitoring"] = False
    await update.message.reply_text("🛑 Surveillance arrêtée.")


@restricted
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state["search_url"]:
        await update.message.reply_text("⚠️ Choisissez d'abord une zone avec /ville ou /carte.")
        return
    await update.message.reply_text("🔄 Vérification en cours…")
    result = check_listings(state["search_url"], chat_id)
    status = result["status"]
    detail = result["detail"]
    code = result.get("http_code")

    emoji = {"ok": "✅", "blocked": "⛔", "rate_limited": "🚫", "captcha": "🤖", "empty_page": "📭", "error": "⚠️"}.get(status, "❓")
    msg = f"{emoji} <b>{status.upper()}</b>\n{detail}\nHTTP: {code if code else 'N/A'}"
    if status == "ok":
        msg += f"\n\n👉 <a href=\"{state['search_url']}\">Voir les résultats</a>"
    await update.message.reply_html(msg)


@restricted
async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    await update.message.reply_text("🔔 Déclenchement d'une alerte de test…")
    send_ntfy_alarm(chat_id, "[TEST] Logement fictif")
    await send_alarm(
        chat_id,
        f"<b>[TEST] NOUVEAU LOGEMENT CROUS !</b>\n"
        f"Ceci est une notification de test.\n\n"
        f"👉 <a href=\"{state['search_url'] or CROUS_SEARCH_BASE_URL}\">Voir les résultats</a>"
    )


@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_state(update.effective_chat.id)
    last = s["last_check"].strftime("%d/%m %H:%M:%S") if s["last_check"] else "jamais"
    known = len(s["known_ids"])
    await update.message.reply_html(
        f"📊 <b>État du bot</b>\n\n"
        f"Zone         : {s['zone_label'] or 'non définie'}\n"
        f"Surveillance : {'🟢 ON' if s['monitoring'] else '🔴 OFF'}\n"
        f"Bloqué       : {'⛔ OUI' if s['blocked'] else '✅ non'}\n"
        f"Logements vus: {known}\n"
        f"Vérifications: {s['check_count']}\n"
        f"Dernier check: {last}\n"
        f"Intervalle   : toutes les {CHECK_INTERVAL}s"
    )


@restricted
async def on_free_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the follow-up message after /ville or /carte."""
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    text = (update.message.text or "").strip()
    awaiting = state["awaiting"]

    if awaiting == "city":
        state["awaiting"] = None
        await update.message.reply_text(f"🔎 Recherche de « {text} »…")
        geo = geocode_place(text)
        if not geo:
            await update.message.reply_text(
                "❌ Aucune ville ou département trouvé avec ce nom. Réessayez avec /ville, "
                "ou utilisez /carte pour choisir la zone à la main."
            )
            return
        url = build_search_url(geo["bounds"])
        state["search_url"] = url
        state["zone_label"] = geo["label"]
        state["known_ids"] = set()
        await update.message.reply_html(
            f"✅ Zone définie : <b>{geo['label']}</b>\n🔗 {url}\n\nUtilisez /monitor pour démarrer la surveillance."
        )
        return

    if awaiting == "url":
        state["awaiting"] = None
        if "trouverunlogement.lescrous.fr" not in text or "bounds=" not in text:
            await update.message.reply_text(
                "❌ Cette URL ne ressemble pas à un lien de recherche CROUS valide "
                "(il doit contenir bounds=...). Réessayez avec /carte."
            )
            return
        state["search_url"] = text
        state["zone_label"] = "zone choisie sur la carte"
        state["known_ids"] = set()
        await update.message.reply_html(
            f"✅ Zone définie à partir de votre lien.\n🔗 {text}\n\nUtilisez /monitor pour démarrer la surveillance."
        )
        return

    await update.message.reply_text("Utilisez /help pour voir les commandes disponibles.")


# ── Bot bootstrap ──────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("start",   "Démarrer le bot / obtenir mon chat ID"),
            ("help",    "Afficher la liste des commandes"),
            ("ville",   "Choisir une zone en tapant une ville/département"),
            ("carte",   "Choisir une zone sur la carte interactive"),
            ("zone",    "Voir la zone actuellement configurée"),
            ("monitor", "Démarrer la surveillance"),
            ("stop",    "Arrêter la surveillance"),
            ("check",   "Vérifier immédiatement"),
            ("status",  "Voir l'état du bot"),
            ("test",    "Déclencher une alerte de test"),
            ("reset",   "Effacer la zone choisie"),
        ]
    )


def create_flask_app(telegram_application: Application) -> Flask:
    """Create Flask app for webhook handling."""
    flask_app = Flask(__name__)

    @flask_app.route("/health", methods=["GET"])
    def health():
        return {"status": "ok"}, 200

    @flask_app.route(WEBHOOK_PATH, methods=["POST"])
    async def telegram_webhook():
        """Handle incoming Telegram updates via webhook."""
        try:
            update_data = flask_request.get_json()
            if update_data:
                update = Update.de_json(update_data, telegram_application.bot)
                await telegram_application.process_update(update)
        except Exception as exc:
            logger.error(f"Error processing update: {exc}")
        return "OK", 200

    return flask_app


async def main_async():
    """Async main function."""
    global telegram_app

    if not BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Create a .env file with:\n"
            "  TELEGRAM_BOT_TOKEN=<your token>\n"
        )
        sys.exit(1)

    telegram_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    telegram_app.add_handler(CommandHandler("start",   cmd_start))
    telegram_app.add_handler(CommandHandler("help",    cmd_help))
    telegram_app.add_handler(CommandHandler("ville",   cmd_ville))
    telegram_app.add_handler(CommandHandler("carte",   cmd_carte))
    telegram_app.add_handler(CommandHandler("zone",    cmd_zone))
    telegram_app.add_handler(CommandHandler("monitor", cmd_monitor))
    telegram_app.add_handler(CommandHandler("stop",    cmd_stop))
    telegram_app.add_handler(CommandHandler("check",   cmd_check))
    telegram_app.add_handler(CommandHandler("status",  cmd_status))
    telegram_app.add_handler(CommandHandler("test",    cmd_test))
    telegram_app.add_handler(CommandHandler("reset",   cmd_reset))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_free_text))

    logger.info("Starting Telegram bot in webhook mode…")
    
    # Set webhook
    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}{WEBHOOK_PATH}"
    logger.info(f"Setting webhook to: {webhook_url}")
    try:
        await telegram_app.bot.set_webhook(url=webhook_url)
    except Exception as exc:
        logger.error(f"Failed to set webhook: {exc}")

    # Start bot with webhook
    await telegram_app.initialize()
    await telegram_app.start()

    logger.info("Bot running in webhook mode")
    
    # Keep the bot running (Render will manage the process)
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        await telegram_app.stop()


if __name__ == "__main__":
    import asyncio as aio

    # Create Flask app (for Render)
    telegram_app_temp = None
    
    async def setup_and_run():
        global telegram_app
        
        if not BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN is not set.")
            sys.exit(1)

        telegram_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

        telegram_app.add_handler(CommandHandler("start",   cmd_start))
        telegram_app.add_handler(CommandHandler("help",    cmd_help))
        telegram_app.add_handler(CommandHandler("ville",   cmd_ville))
        telegram_app.add_handler(CommandHandler("carte",   cmd_carte))
        telegram_app.add_handler(CommandHandler("zone",    cmd_zone))
        telegram_app.add_handler(CommandHandler("monitor", cmd_monitor))
        telegram_app.add_handler(CommandHandler("stop",    cmd_stop))
        telegram_app.add_handler(CommandHandler("check",   cmd_check))
        telegram_app.add_handler(CommandHandler("status",  cmd_status))
        telegram_app.add_handler(CommandHandler("test",    cmd_test))
        telegram_app.add_handler(CommandHandler("reset",   cmd_reset))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_free_text))

        # Initialize
        await telegram_app.initialize()

        # Create Flask app
        flask_app = Flask(__name__)

        @flask_app.route("/health", methods=["GET"])
        def health():
            return {"status": "ok"}, 200

        @flask_app.route(WEBHOOK_PATH, methods=["POST"])
        def telegram_webhook():
            try:
                update_data = flask_request.get_json()
                if update_data:
                    update = Update.de_json(update_data, telegram_app.bot)
                    # Run async function in sync context
                    asyncio.run(telegram_app.process_update(update))
            except Exception as exc:
                logger.error(f"Error processing update: {exc}")
            return "OK", 200

        # Set webhook
        webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}{WEBHOOK_PATH}"
        logger.info(f"Setting webhook to: {webhook_url}")
        try:
            await telegram_app.bot.set_webhook(url=webhook_url)
            logger.info("✅ Webhook set successfully")
        except Exception as exc:
            logger.error(f"❌ Failed to set webhook: {exc}")

        # Run Flask on the port Render assigns
        port = int(os.getenv("PORT", 5000))
        logger.info(f"Starting Flask server on port {port}…")
        
        # Run Flask in a thread and keep bot alive
        import threading
        flask_thread = threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=port, debug=False),
            daemon=True
        )
        flask_thread.start()

        # Keep the bot alive
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down…")
            await telegram_app.stop()

    aio.run(setup_and_run())
