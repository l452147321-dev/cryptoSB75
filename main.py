"""
Bot Telegram de liens de paiement crypto (NOWPayments).

Parcours en message privé :
  /start ou /vente -> nom d'agent ou TeamLeader -> nom du joueur -> montant en euros
  -> le bot renvoie un lien de paiement crypto à transmettre au client.

Commandes : /start, /vente, /annuler, /mesventes, /recap [semaine|mois|tout] (admin)

Variables d'environnement :
  TELEGRAM_BOT_TOKEN      (obligatoire) jeton donné par @BotFather
  ADMIN_CHAT_ID           (obligatoire) identifiant du groupe admin (ex. -1001234567890)
  PUBLIC_URL              (obligatoire) domaine public Railway (ex. https://xxx.up.railway.app)
  WEBHOOK_SECRET          (obligatoire) longue chaîne aléatoire (lettres et chiffres)
  NOWPAYMENTS_API_KEY     (obligatoire) clé API NOWPayments
  NOWPAYMENTS_IPN_SECRET  (obligatoire) clé secrète IPN NOWPayments
  TEST_MODE               (optionnelle) "true" = bac à sable NOWPayments, "false" = réel
  AGENTS                  (optionnelle) identifiants Telegram autorisés, séparés par des virgules
  PAY_CURRENCY            (optionnelle) impose une crypto (ex. usdttrc20) ; vide = au choix du client
  DB_PATH                 (optionnelle) chemin de la base, par défaut /data/ventes.db
"""

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
if not PUBLIC_URL.startswith("http"):
    PUBLIC_URL = "https://" + PUBLIC_URL
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
NOWPAYMENTS_API_KEY = os.environ["NOWPAYMENTS_API_KEY"]
NOWPAYMENTS_IPN_SECRET = os.environ["NOWPAYMENTS_IPN_SECRET"]
TEST_MODE = os.environ.get("TEST_MODE", "true").strip().lower() in ("1", "true", "yes", "oui")
PAY_CURRENCY = os.environ.get("PAY_CURRENCY", "").strip().lower()
DB_PATH = os.environ.get("DB_PATH", "/data/ventes.db")
AGENTS = {
    a.strip().lstrip("@").lower()
    for a in os.environ.get("AGENTS", "").split(",")
    if a.strip()
}

NOWPAYMENTS_API_URL = os.environ.get(
    "NOWPAYMENTS_API_URL",
    "https://api-sandbox.nowpayments.io/v1" if TEST_MODE else "https://api.nowpayments.io/v1",
).rstrip("/")

PARIS = ZoneInfo("Europe/Paris")
MONTANT_MIN = 5.0
MONTANT_MAX = 50000.0

AGENT, JOUEUR, MONTANT = range(3)

# --------------------------------------------------------------------------- #
# Base de données (SQLite)
# --------------------------------------------------------------------------- #


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    dossier = os.path.dirname(DB_PATH)
    if dossier:
        os.makedirs(dossier, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ventes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                reference    TEXT UNIQUE NOT NULL,
                agent        TEXT NOT NULL,
                joueur       TEXT NOT NULL,
                montant      REAL NOT NULL,
                tg_id        INTEGER NOT NULL,
                tg_compte    TEXT,
                invoice_id   TEXT,
                invoice_url  TEXT,
                payment_id   TEXT,
                crypto       TEXT,
                statut       TEXT NOT NULL DEFAULT 'en_attente',
                test         INTEGER NOT NULL DEFAULT 0,
                cree_le      TEXT NOT NULL,
                paye_le      TEXT
            )
            """
        )


def maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Outils
# --------------------------------------------------------------------------- #


def e(texte) -> str:
    return html.escape(str(texte))


def euros(montant: float) -> str:
    return f"{montant:,.2f}".replace(",", " ").replace(".", ",") + " €"


def compte_telegram(user) -> str:
    return f"@{user.username}" if user.username else f"{user.full_name} (id {user.id})"


def autorise(user) -> bool:
    if not AGENTS:
        return True
    return str(user.id) in AGENTS or (user.username or "").lower() in AGENTS


async def est_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Admin = commande envoyée dans le groupe admin, ou par un membre de ce groupe."""
    if update.effective_chat.id == ADMIN_CHAT_ID:
        return True
    try:
        membre = await context.bot.get_chat_member(ADMIN_CHAT_ID, update.effective_user.id)
        return membre.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


def etiquette_test() -> str:
    return "🧪 <b>MODE TEST</b>\n" if TEST_MODE else ""


# --------------------------------------------------------------------------- #
# NOWPayments
# --------------------------------------------------------------------------- #


async def creer_facture(reference: str, agent: str, joueur: str, montant: float) -> dict:
    corps = {
        "price_amount": montant,
        "price_currency": "eur",
        "order_id": reference,
        "order_description": f"{agent} / {joueur}"[:150],
        "ipn_callback_url": f"{PUBLIC_URL}/webhook/nowpayments/{WEBHOOK_SECRET}",
    }
    if PAY_CURRENCY:
        corps["pay_currency"] = PAY_CURRENCY
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{NOWPAYMENTS_API_URL}/invoice",
            json=corps,
            headers={"x-api-key": NOWPAYMENTS_API_KEY},
        )
    if r.status_code >= 400:
        log.error("NOWPayments création facture %s : %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()


async def lire_paiement(payment_id: str) -> dict:
    """Revérifie le paiement directement auprès de NOWPayments."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{NOWPAYMENTS_API_URL}/payment/{payment_id}",
            headers={"x-api-key": NOWPAYMENTS_API_KEY},
        )
    r.raise_for_status()
    return r.json()


_MARQUE = "\x00N"
_RE_MARQUE = re.compile(r'"\\u0000N(.*?)\\u0000N"')


def signature_valide(corps_brut: bytes, signature: str) -> bool:
    """Signature IPN : HMAC-SHA512 du JSON trié par clés, avec la clé secrète IPN.

    Les nombres sont conservés tels qu'ils ont été reçus pour que le JSON
    recalculé soit identique à celui signé par NOWPayments.
    """
    if not signature:
        return False
    try:
        garde = lambda s: f"{_MARQUE}{s}{_MARQUE}"  # noqa: E731
        donnees = json.loads(corps_brut, parse_float=garde, parse_int=garde)
        trie = json.dumps(donnees, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        trie = _RE_MARQUE.sub(r"\1", trie)
    except Exception:
        return False
    attendu = hmac.new(
        NOWPAYMENTS_IPN_SECRET.strip().encode(), trie.encode("utf-8"), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(attendu, signature.strip().lower())


# --------------------------------------------------------------------------- #
# Conversation : création d'un lien
# --------------------------------------------------------------------------- #


async def debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Écris-moi en message privé pour créer un lien de paiement.")
        return ConversationHandler.END
    if not autorise(update.effective_user):
        await update.message.reply_text(
            "⛔ Tu n'es pas autorisé à utiliser ce bot.\n"
            f"Ton identifiant Telegram : {update.effective_user.id}"
        )
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Nouvelle vente (paiement crypto).\n\n"
        "Quel est ton nom d'agent ou TeamLeader ?\n\n"
        "(/annuler pour arrêter)"
    )
    return AGENT


async def recu_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nom = update.message.text.strip()
    if not 1 <= len(nom) <= 60:
        await update.message.reply_text("Nom invalide. Renvoie ton nom d'agent ou TeamLeader.")
        return AGENT
    context.user_data["agent"] = nom
    await update.message.reply_text("Nom du joueur ?")
    return JOUEUR


async def recu_joueur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nom = update.message.text.strip()
    if not 1 <= len(nom) <= 60:
        await update.message.reply_text("Nom invalide. Renvoie le nom du joueur.")
        return JOUEUR
    context.user_data["joueur"] = nom
    await update.message.reply_text("Montant en euros ? (exemple : 150)")
    return MONTANT


async def recu_montant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    brut = update.message.text.strip().replace("€", "").replace(" ", "").replace(",", ".")
    try:
        montant = round(float(brut), 2)
    except ValueError:
        await update.message.reply_text("Montant invalide. Envoie seulement un nombre, exemple : 150")
        return MONTANT
    if not MONTANT_MIN <= montant <= MONTANT_MAX:
        await update.message.reply_text(
            f"Le montant doit être entre {euros(MONTANT_MIN)} et {euros(MONTANT_MAX)}."
        )
        return MONTANT

    user = update.effective_user
    agent = context.user_data["agent"]
    joueur = context.user_data["joueur"]
    reference = "CR-" + secrets.token_hex(4).upper()

    await update.message.reply_text("⏳ Création du lien…")
    try:
        facture = await creer_facture(reference, agent, joueur, montant)
        lien = facture["invoice_url"]
        invoice_id = str(facture.get("id", ""))
    except Exception:
        log.exception("Échec de création de facture")
        await update.message.reply_text(
            "❌ Impossible de créer le lien pour le moment. Réessaie avec /vente.\n"
            "Si le montant est très petit, essaie un montant plus élevé."
        )
        context.user_data.clear()
        return ConversationHandler.END

    with db() as conn:
        conn.execute(
            "INSERT INTO ventes (reference, agent, joueur, montant, tg_id, tg_compte,"
            " invoice_id, invoice_url, test, cree_le) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (reference, agent, joueur, montant, user.id, compte_telegram(user),
             invoice_id, lien, int(TEST_MODE), maintenant()),
        )

    await update.message.reply_text(
        f"{etiquette_test()}"
        "✅ <b>Lien de paiement créé</b>\n\n"
        f"TeamLeader ou agent : {e(agent)}\n"
        f"Joueur : {e(joueur)}\n"
        f"Montant : {euros(montant)}\n"
        f"Référence : <code>{reference}</code>\n\n"
        f"Lien à transmettre au client :\n{e(lien)}\n\n"
        "Tu seras prévenu dès que le paiement sera confirmé.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"{etiquette_test()}"
            "🆕 <b>Lien crypto créé</b>\n"
            f"TeamLeader ou agent : {e(agent)}\n"
            f"Joueur : {e(joueur)}\n"
            f"Montant : {euros(montant)}\n"
            f"Compte Telegram : {e(compte_telegram(user))}\n"
            f"Référence : <code>{reference}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("Message admin non envoyé (création)")

    context.user_data.clear()
    return ConversationHandler.END


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Annulé. Envoie /vente pour recommencer.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /mesventes et /recap
# --------------------------------------------------------------------------- #

STATUTS = {
    "en_attente": "⏳ en attente",
    "paye": "✅ payé",
    "partiel": "⚠️ partiel",
    "echec": "❌ échec",
    "expire": "⌛ expiré",
}


async def mes_ventes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    with db() as conn:
        lignes = conn.execute(
            "SELECT * FROM ventes WHERE tg_id = ? ORDER BY id DESC LIMIT 15", (user.id,)
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(montant), 0) AS s FROM ventes"
            " WHERE tg_id = ? AND statut = 'paye' AND test = 0",
            (user.id,),
        ).fetchone()
    if not lignes:
        await update.message.reply_text("Aucune vente pour l'instant. Envoie /vente pour commencer.")
        return
    texte = ["<b>Tes 15 dernières ventes</b>\n"]
    for v in lignes:
        jour = datetime.fromisoformat(v["cree_le"]).astimezone(PARIS).strftime("%d/%m")
        marque = " 🧪" if v["test"] else ""
        texte.append(
            f"{jour} · {e(v['joueur'])} · {euros(v['montant'])} · "
            f"{STATUTS.get(v['statut'], v['statut'])}{marque}"
        )
    texte.append(f"\n<b>Total payé : {euros(total['s'])}</b> ({total['n']} ventes)")
    await update.message.reply_text("\n".join(texte), parse_mode=ParseMode.HTML)


async def recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await est_admin(update, context):
        await update.message.reply_text("⛔ Commande réservée aux admins.")
        return
    periode = (context.args[0].lower() if context.args else "semaine")
    ici = datetime.now(PARIS)
    if periode in ("semaine", "week"):
        depuis = (ici - timedelta(days=ici.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        titre = "cette semaine"
    elif periode in ("mois", "month"):
        depuis = ici.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        titre = "ce mois"
    elif periode in ("tout", "all"):
        depuis = datetime(2000, 1, 1, tzinfo=PARIS)
        titre = "depuis le début"
    else:
        await update.message.reply_text("Utilisation : /recap semaine, /recap mois ou /recap tout")
        return
    borne = depuis.astimezone(timezone.utc).isoformat(timespec="seconds")
    with db() as conn:
        lignes = conn.execute(
            "SELECT agent, COUNT(*) AS n, SUM(montant) AS s FROM ventes"
            " WHERE statut = 'paye' AND test = 0 AND paye_le >= ?"
            " GROUP BY agent COLLATE NOCASE ORDER BY s DESC",
            (borne,),
        ).fetchall()
    if not lignes:
        await update.message.reply_text(f"Aucune vente payée {titre}.")
        return
    texte = [f"📊 <b>Récap crypto — {titre}</b>\n"]
    for l in lignes:
        texte.append(f"{e(l['agent'])} : {euros(l['s'])} ({l['n']})")
    texte.append(
        f"\n<b>Total : {euros(sum(l['s'] for l in lignes))}</b> "
        f"({sum(l['n'] for l in lignes)} ventes)"
    )
    await update.message.reply_text("\n".join(texte), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Application Telegram + serveur web
# --------------------------------------------------------------------------- #

tg = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()

texte_simple = filters.TEXT & ~filters.COMMAND
tg.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler(["start", "vente"], debut)],
        states={
            AGENT: [MessageHandler(texte_simple, recu_agent)],
            JOUEUR: [MessageHandler(texte_simple, recu_joueur)],
            MONTANT: [MessageHandler(texte_simple, recu_montant)],
        },
        fallbacks=[
            CommandHandler("annuler", annuler),
            CommandHandler(["start", "vente"], debut),
        ],
        allow_reentry=True,
    )
)
tg.add_handler(CommandHandler("annuler", annuler))
tg.add_handler(CommandHandler("mesventes", mes_ventes))
tg.add_handler(CommandHandler("recap", recap))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await tg.initialize()
    await tg.start()
    await tg.bot.set_webhook(
        url=f"{PUBLIC_URL}/webhook/telegram",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
    )
    log.info("Bot démarré (mode %s) — API %s", "TEST" if TEST_MODE else "RÉEL", NOWPAYMENTS_API_URL)
    yield
    await tg.stop()
    await tg.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def sante():
    return {"ok": True}


@app.post("/webhook/telegram")
async def webhook_telegram(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    update = Update.de_json(await request.json(), tg.bot)
    await tg.process_update(update)
    return {"ok": True}


@app.post("/webhook/nowpayments/{secret}")
async def webhook_nowpayments(
    secret: str,
    request: Request,
    x_nowpayments_sig: str = Header(default=""),
):
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    corps = await request.body()
    if not signature_valide(corps, x_nowpayments_sig):
        log.warning("IPN refusé : signature invalide")
        raise HTTPException(status_code=403)

    payment_id = json.loads(corps).get("payment_id")
    if not payment_id:
        return {"ok": True}

    # On ne fait jamais confiance au contenu du webhook :
    # on relit le paiement directement chez NOWPayments.
    try:
        paiement = await lire_paiement(str(payment_id))
    except Exception:
        log.exception("Revérification impossible pour le paiement %s", payment_id)
        raise HTTPException(status_code=503)  # NOWPayments renverra la notification

    reference = str(paiement.get("order_id") or "")
    statut = str(paiement.get("payment_status") or "").lower()
    crypto = str(paiement.get("pay_currency") or "").upper()

    with db() as conn:
        vente = conn.execute("SELECT * FROM ventes WHERE reference = ?", (reference,)).fetchone()
    if not vente:
        log.warning("IPN pour une référence inconnue : %s", reference)
        return {"ok": True}

    try:
        montant_ok = (
            abs(float(paiement.get("price_amount") or 0) - vente["montant"]) < 0.01
            and str(paiement.get("price_currency") or "").lower() == "eur"
        )
    except (TypeError, ValueError):
        montant_ok = False

    if statut == "finished" and montant_ok:
        with db() as conn:
            cur = conn.execute(
                "UPDATE ventes SET statut = 'paye', paye_le = ?, payment_id = ?, crypto = ?"
                " WHERE reference = ? AND statut != 'paye'",
                (maintenant(), str(payment_id), crypto, reference),
            )
            nouveau = cur.rowcount == 1
        if nouveau:  # une seule notification, même si le webhook arrive plusieurs fois
            marque = "🧪 <b>MODE TEST</b>\n" if vente["test"] else ""
            await _envoyer(
                ADMIN_CHAT_ID,
                f"{marque}"
                "💰 <b>Paiement crypto confirmé</b>\n"
                f"TeamLeader ou agent : {e(vente['agent'])}\n"
                f"Joueur : {e(vente['joueur'])}\n"
                f"Montant : {euros(vente['montant'])}\n"
                f"Crypto : {e(crypto)}\n"
                f"Compte Telegram : {e(vente['tg_compte'])}\n"
                f"Référence : <code>{reference}</code>",
            )
            await _envoyer(
                vente["tg_id"],
                f"{marque}"
                "💰 <b>Paiement confirmé</b>\n"
                f"Joueur : {e(vente['joueur'])}\n"
                f"Montant : {euros(vente['montant'])}\n"
                f"Référence : <code>{reference}</code>",
            )
    elif statut == "finished" and not montant_ok:
        log.warning("Montant incohérent pour %s : %s", reference, paiement)
        await _envoyer(
            ADMIN_CHAT_ID,
            f"⚠️ Paiement reçu mais montant incohérent — référence <code>{reference}</code>. À vérifier.",
        )
    elif statut == "partially_paid" and vente["statut"] == "en_attente":
        with db() as conn:
            conn.execute("UPDATE ventes SET statut = 'partiel', payment_id = ? WHERE reference = ?",
                         (str(payment_id), reference))
        await _envoyer(
            ADMIN_CHAT_ID,
            "⚠️ <b>Paiement partiel</b> (le client a envoyé moins que prévu)\n"
            f"TeamLeader ou agent : {e(vente['agent'])}\n"
            f"Joueur : {e(vente['joueur'])}\n"
            f"Montant attendu : {euros(vente['montant'])}\n"
            f"Reçu : {e(paiement.get('actually_paid'))} {e(crypto)}\n"
            f"Référence : <code>{reference}</code>",
        )
    elif statut in ("failed", "expired", "refunded") and vente["statut"] == "en_attente":
        with db() as conn:
            conn.execute(
                "UPDATE ventes SET statut = ? WHERE reference = ?",
                ("expire" if statut == "expired" else "echec", reference),
            )
    return {"ok": True}


async def _envoyer(chat_id: int, texte: str) -> None:
    try:
        await tg.bot.send_message(chat_id, texte, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Message non envoyé à %s", chat_id)
