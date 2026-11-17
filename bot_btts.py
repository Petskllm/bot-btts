import csv
import json
import math
import os
import re
import time
import unicodedata
import itertools
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI — VERSION 6.6.2
#
# Marchés :
# - Domicile +0,5 but (72-85 %)
# - Extérieur +0,5 but (72-85 %)
# - BTTS Oui (72-85 %)
# - Plus de 1,5 but (72-85 %)
# - Équipe : plus de 2,5 tirs cadrés (70 % minimum)
#
# Correction 6.2.1 :
# - Les tirs cadrés sont associés aux identifiants réels des équipes
# - Suppression buteurs et H2H
# - Cache disque des statistiques de tirs cadrés
#
# Corrections 6.2.2 :
# - Vérification des résultats : on traite les dates récentes (les vieilles
#   lignes introuvables ne bloquent plus les nouvelles) et on les expire
# - Probabilité tirs cadrés : vraie loi de Poisson pour "3 ou plus"
# - Un match avec 0 tir cadré n'est plus traité comme une donnée absente
# - Plus de plancher sur la probabilité "extérieur marque"
# - Tirs cadrés : enregistrés dans le journal mais NON publiés (non validés)
#
# Version 6.3.0 :
# - Ticket du jour (4 matchs, cote juste visée ~10) choisi sur la forme des
#   5 derniers matchs + probabilité 50-72 %, tous marchés buts
# - Nouveaux marchés journalisés : victoire, +1,5 but équipe, +2,5 buts
# - Journal : on enregistre dès 50 % (published = 2 pour le ticket)
#
# Version 6.4.0 (revue complète) :
# Corrections :
# 1. API : un 404 (stats absentes d'un petit championnat, slug inconnu) ne
#    compte plus comme un "échec d'affilée". Avant, 8 matchs sans stats de
#    tirs cadrés suffisaient à bloquer TOUTE l'analyse.
# 2. Tirs cadrés : les stats d'un ancien match étaient lues avec les
#    identifiants du match du jour (mauvais adversaire, et mauvaise équipe
#    pour l'extérieur). On garde maintenant les vrais home_id / away_id.
# 3. Tirs cadrés : le lieu (domicile / extérieur) se décalait dès qu'un
#    match sans stats était sauté.
# 4. Ticket : "équipe +0,5 but" n'avait pas de score de forme et ne pouvait
#    donc jamais entrer dans le ticket.
# 5. Ticket : si la cote cible n'est pas atteinte, on prend la combinaison
#    la mieux notée (avant : la plus grosse cote = la plus risquée).
# 6. Telegram : nouvel essai (réseau / 429 / 5xx), plus de plantage si la
#    réponse n'est pas du JSON, un seul envoi par jour (FORCE_SEND=1).
# 7. Journal : un en-tête différent ne fait plus repartir de zéro,
#    écriture atomique (.tmp puis remplacement), migration 6.3 -> 6.4.
# Mineurs : messages d'erreur complets, User-Agent à jour, constante morte
# AWAY_PROB_FLOOR signalée (gardée), bonus domicile appliqué une seule fois, ids des équipes
# enregistrés dans le journal (vérification tirs cadrés).
# Améliorations :
# 8. Filtre de forme (5 derniers matchs des 2 équipes) : pas de victoire
#    d'une équipe sur 2 défaites d'affilée, pas de "+0,5 but" pour une
#    équipe muette 2 fois de suite, l'équipe doit marquer dans >= 3/5 et
#    l'adversaire encaisser dans >= 3/5, BTTS / +1,5 / +2,5 vérifiés aussi.
# 9. Cote minimale par jambe : jamais sous MIN_LEG_ODDS (1,25).
# 10. Bilan de rentabilité : réussite et ROI par marché (cote jouée si la
#     colonne odds_taken est remplie, sinon cote min), calibration par
#     tranche, score de Brier, fichier data/bilan.csv.
# 11. Forme V/N/D affichée sous chaque pronostic.
# 12. Tirs cadrés calculés seulement s'il reste du budget API.
# 14. Enjeu (motivation) d'après le classement déjà lu (standings) :
#     affiché, et un léger bonus/malus dans la note du ticket.
#     Cotes bookmaker et blessures : voir TODO (endpoint non utilisé ici).
#
# Version 6.5.0 (d'après ton bilan 30 jours) :
# - Enjeu / classement : affichés UNIQUEMENT pour un vrai championnat
#   (format "league", pas de groupes, pas de phase/stage, saison en cours,
#   tableau de 8 à 30 équipes, les 2 équipes dedans, au moins 3 matchs
#   joués). Plus de "🔥 titre/Europe" sur les sélections, Ligue des nations,
#   coupes, ni de "2e/1er sur 64". Dans le doute, la ligne n'est pas affichée.
# - "Domicile +0,5" mis en avant (89 % réussi pour 81 % annoncé, ROI +14 %) :
#   petit bonus de classement (MARKET_RANK_BONUS) dans les pronos et le
#   ticket. La probabilité affichée et la cote min ne changent pas, et un
#   pari doit toujours passer les filtres (72-85 %, forme, cote >= 1,25).
#   "Extérieur +0,5" (ROI +6 %) reçoit un plus petit bonus.
# - BTTS n'est plus proposé (1/3, ROI -53 %) ni dans les pronos ni dans le
#   ticket. Il reste calculé et enregistré dans le journal (published = 0)
#   pour suivre sa calibration ; les anciennes lignes BTTS restent lues.
#
# Version 6.6.0 (offre gratuite SportAPI, 3 lancements par jour) :
# - Cache disque du jour (data/cache/, purgé après 3 jours) : équipes,
#   classements, H2H, buteurs, compos... Les 2e et 3e lancements relisent
#   le cache et ne paient presque plus d'appels. Les stats / compos /
#   événements d'un match terminé sont gardés pour toujours (ils ne
#   changent plus). Les matchs du jour sont redemandés avec ETag (304).
# - Quota : lecture de X-RateLimit-Remaining / X-RateLimit-Month-Remaining.
#   Budget du jour = quota du mois restant / jours restants (sinon 230),
#   85 % pour le 1er lancement, le reste pour les suivants. Arrêt propre
#   avant d'épuiser le quota (jamais de 429 "mois").
# - Message Telegram ULTRA léger (format validé) : une ligne par pick,
#   une ligne d'alerte seulement s'il y en a une, ticket en 2 lignes,
#   bilan en 1 ligne. Le détail complet (forme, buts dom/ext, clean
#   sheets, matchs sans marquer, repos, H2H, enjeu...) est écrit dans
#   data/fiches/AAAA-MM-JJ_HHMM.txt.
# - Infos par pick (sans appel en plus) : buts pour / contre à domicile
#   et à l'extérieur (5 derniers), % clean sheets, % matchs sans marquer,
#   jours de repos (+ alerte si <= 3 jours).
# - H2H (5 derniers) pour les picks publiés seulement : alerte si l'équipe
#   a peu marqué contre cet adversaire.
# - Indices (jamais affirmés) : meilleur buteur absent de la compo du
#   dernier match, joueur expulsé au dernier match. Seulement si le budget
#   le permet, sinon rien n'est affiché.
# - Ticket : toujours 4 matchs, cote 10 minimum ; recherche élargie
#   (2 marchés max par match dans la sélection, puis réserve plus large)
#   avant de dire "cote 10 non atteinte".
# - Cotes réelles (optionnel) : seulement si la variable ODDS_API_KEY
#   existe (The Odds API, 500 crédits gratuits / mois). Sinon aucune
#   ligne et aucun appel. Max 7 appels (14 crédits) par jour, cache du jour.
#   Cote affichée seulement pour +1,5 / +2,5 buts et victoire (le marché
#   "équipe +0,5 but" n'existe pas chez eux).
# - Jamais d'info inventée : une ligne sans donnée n'est pas affichée.
#
# Version 6.6.1 (message Telegram, format choisi par Betpulse) :
# - Chaque pick affiche la vraie cote (The Odds API) quand elle existe pour
#   ce marché, sinon la cote min du bot précédée de "~" (estimation).
#   "✅ value" seulement si la vraie cote dépasse la cote min du bot.
# - Ticket : "🎟️ TICKET · cote X", jambes séparées par " / ".
# - Une seule ligne d'alerte courte "⚠️ ..." : buteur absent, expulsé,
#   mauvais H2H, et repos seulement si <= 2 jours (avant : 3, trop souvent).
# - Noms courts : sélections nationales ("Football Union of Russia" ->
#   "Russia", "Bosnia-Herzegovina" -> "Bosnia"...) ; dans les libellés
#   "match" (Dom-Ext), Town / United / City / FC sont retirés.
# - Bilan : "📊 30j : 77% · ROI 0%" (plus de "-0%").
#
# Version 6.6.2 (affichage Telegram, modèle de Betpulse) :
# - Retour à la présentation par heure (⏰ 14h) et par match :
#   "N. Dom — Ext · HH:MM", "🏆 Ligue", une ligne par marché
#   "• Marché : 83% · 💰 min 1,25" (ou "💰 cote 1,40 ✅ value" si vraie
#   cote), "⚽ Buts projetés", et UNE ligne "⚠️ ..." seulement s'il y a
#   une vraie alerte. Tous les marchés retenus du match sont affichés.
# - Plus de forme récente / enjeu / classement dans Telegram (gardés dans
#   la fiche data/fiches/).
# - Ticket : même présentation par jambe ("cote juste" ou vraie cote),
#   puis la cote totale ou "cote 10 non atteinte (meilleur X)".
# - Découpage Telegram par blocs de match, sous la limite de 4096.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROB = 0.72
MAX_PROB = 0.85

SOT_LINE = 2.5
SOT_MIN_PROB = 0.70
SOT_MAX_PROB = 0.92
SOT_HISTORY_N = 5
SOT_CACHE_FILE = "data/sot_stats_cache.json"

ODDS_MARGIN = 1.03
LOG_MIN_PROB = 0.50

MAX_MATCHES = 15

TICKET_LEGS = 4
TICKET_MIN_PROB = 0.50
TICKET_MAX_PROB = 0.72
TICKET_TARGET_ODDS = 10.0
TICKET_FORM_N = 5
TICKET_POOL = 24
TICKET_FORM_WEIGHT = 0.40
MAX_MARKETS = 3

MAX_PER_LEAGUE = 12
PRIORITY_LEAGUE_IDS = set()

EXCLUDE_YOUTH = True
YOUTH_PATTERN = re.compile(
    r"\bU[- ]?(1[5-9]|2[0-3])\b|\byouth\b|\bjuniors?\b"
    r"|\bprimavera\b|\breserves?\b|\bamateurs?\b|\bII$",
    re.IGNORECASE,
)

PRIOR_K = 4.0
PRIORS = {
    "gf": 1.35,
    "ga": 1.35,
    "btts": 0.52,
    "over15": 0.75,
}

HISTORY_N = 10
MIN_HISTORY = 6
VENUE_N = 6
MAX_HISTORY_DAYS = 240
MAX_STALE_DAYS = 60
DECAY = 0.90

HOME_ADJUST = 0.02
AWAY_ADJUST = -0.02
HOME_PROB_CAP = 0.88
AWAY_PROB_FLOOR = 0.60  # plus utilisé depuis 6.2.2, gardé pour compatibilité

# ---- Filtre de forme (5 derniers matchs) : la forme prime sur le modèle ----
FORM_MIN_SCORED = 3      # l'équipe a marqué dans au moins 3 des 5 derniers
FORM_MIN_CONCEDED = 3    # l'adversaire a encaissé dans au moins 3 des 5
LOSING_STREAK_MAX = 2    # 2 défaites d'affilée -> pas de victoire sèche
DRY_STREAK_MAX = 2       # 2 matchs sans marquer -> pas de "+0,5 but"
FORM_MIN_RATE = 0.40     # BTTS / +1,5 / +2,5 : au moins 40 % sur les 2 équipes

# ---- Cote minimale par jambe : refus sous 1,20, marge de sécurité 1,25 ----
MIN_LEG_ODDS = 1.25

# ---- Tirs cadrés (non publiés) : seulement s'il reste du budget API ----
SOT_BUDGET_SHARE = 0.60

# ---- Enjeu (classement) ----
TOP_ZONE = 4             # 4 premiers : titre / Europe
RELEGATION_ZONE = 3      # 3 derniers : maintien
MOTIVATION_BONUS = 0.03  # bonus / malus sur la note du ticket

# 6.5.0 : enjeu / classement seulement pour un vrai championnat
STANDINGS_MIN_SIZE = 8
STANDINGS_MAX_SIZE = 30   # "sur 64" = tableau de coupe / agrégé -> ignoré
STANDINGS_MIN_PLAYED = 3
NON_LEAGUE_PATTERN = re.compile(
    r"cup|coupe|copa|coppa|pokal|beker|ta[cç]a|trophy|troph[ée]e|shield"
    r"|super ?cup|supercoupe|nations|qualif|friendl|amica|world|mondial"
    r"|euro|africa|afcon|\bcan\b|asian|gold cup|concacaf|conmebol|uefa|fifa"
    r"|\bcaf\b|\bafc\b|\bofc\b|champions league|\bchampions\b|europa|conference league|libertadores"
    r"|sudamericana|play-?offs?|barrage|group|groupe|international",
    re.IGNORECASE,
)

# 6.5.0 : bonus de CLASSEMENT (pas de probabilité) selon ton bilan 30 jours
MARKET_RANK_BONUS = {"home": 0.03, "away": 0.01}
# 6.5.0 : marchés calculés et journalisés mais jamais proposés
NOT_PROPOSED = {"btts"}

# ---- 6.6.0 : cache disque + quota SportAPI (offre gratuite) ----
DISK_CACHE_DIR = "data/cache"
CACHE_KEEP_DAYS = 3
PERMANENT_ENDPOINT = re.compile(r"^fixtures/\d+/(stats|lineups|events)$")
DAILY_BUDGET_DEFAULT = 230   # 7 000 / mois ÷ 30 si l'API ne donne pas son quota
FIRST_RUN_SHARE = 0.85       # le 1er lancement garde 15 % pour les suivants
QUOTA_RESERVE = 15           # on s'arrête avant d'épuiser le quota
ENRICH_RESERVE = 40          # appels gardés pour H2H / buteurs / cartons

# ---- 6.6.0 : infos par pick ----
SHOWN_MARKETS = 10           # 6.6.2 : tous les marchés retenus du match
H2H_N = 5
H2H_ALERT_MAX = 2            # l'équipe a marqué <= 2 fois sur 5 H2H -> alerte
REST_ALERT_DAYS = 2
TOPSCORER_MIN_GOALS = 3
LINEUP_MAX_AGE_DAYS = 14
FICHES_DIR = "data/fiches"
TICKET_POOL_WIDE = 40
TICKET_PER_MATCH = 2

# ---- 6.6.0 : cotes réelles (optionnel, The Odds API) ----
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "eu"
ODDS_MARKETS = "h2h,totals"          # 2 crédits par appel
ODDS_MAX_CALLS_PER_DAY = 7           # 14 crédits / jour ≈ 420 / mois (< 500)
ODDS_MIN_REMAINING = 30
# (motif du nom de ligue SportAPI, pays SportAPI ou None, clé The Odds API)
# Chaque clé est revérifiée à l'exécution dans /v4/sports (appel gratuit).
ODDS_SPORT_KEYS = [
    ("2 bundesliga", "Germany", "soccer_germany_bundesliga2"),
    ("bundesliga", "Germany", "soccer_germany_bundesliga"),
    ("premier league", "England", "soccer_epl"),
    ("championship", "England", "soccer_efl_champ"),
    ("league one", "England", "soccer_england_league1"),
    ("league two", "England", "soccer_england_league2"),
    ("ligue 1", "France", "soccer_france_ligue_one"),
    ("ligue 2", "France", "soccer_france_ligue_two"),
    ("serie a", "Italy", "soccer_italy_serie_a"),
    ("serie b", "Italy", "soccer_italy_serie_b"),
    ("segunda", "Spain", "soccer_spain_segunda_division"),
    ("la liga", "Spain", "soccer_spain_la_liga"),
    ("eredivisie", "Netherlands", "soccer_netherlands_eredivisie"),
    ("primeira", "Portugal", "soccer_portugal_primeira_liga"),
    ("premiership", "Scotland", "soccer_spl"),
    ("super lig", "Turkey", "soccer_turkey_super_league"),
    ("mls", "USA", "soccer_usa_mls"),
    ("champions league", None, "soccer_uefa_champs_league"),
    ("europa league", None, "soccer_uefa_europa_league"),
]

# ---- Bilan / envoi ----
STATS_DAYS = 30
SUMMARY_FILE = "data/bilan.csv"
SENT_FILE = "data/dernier_envoi.txt"
ONE_SEND_PER_DAY = False  # True = un seul envoi par jour (anti-doublon). False = tu peux lancer le bot plusieurs fois par jour

# TODO (non codé, endpoint inconnu dans ce code) :
# - cotes bookmaker : publier seulement si proba modèle > 1 / cote
# - blessures / compositions : baisser l'espérance de buts sans le buteur

MAX_API_CALLS = 350
MAX_SETTLE_DATES = 8
PENDING_EXPIRY_DAYS = 7
MODEL_VERSION = "6.6.2"
MIN_INTERVAL = 0.5
MAX_RUNTIME_SECONDS = 600
TIMEOUT = 20
MAX_RETRIES = 2
RETRY_PAUSE = 60
MAX_CONSECUTIVE_FAILS = 8

PREDICTIONS_FILE = "data/predictions.csv"
FIELDS_V63 = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "prob_base",
    "published", "model_version", "result",
]
# 6.4.0 : home_id / away_id (vérification tirs cadrés) et odds_taken
# (cote réellement jouée, à remplir à la main pour le vrai ROI, ex : 1,45)
FIELDS = FIELDS_V63 + ["home_id", "away_id", "odds_taken"]

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/6.6.2",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
STANDINGS_META = {}

# 6.6.0 : cache disque / quota
DISK = {"day": None, "data": {}, "etag": {}, "dirty": False}
PERM = {"loaded": False, "data": {}, "dirty": False}
USAGE = {}
QUOTA = {}
RUN_CAP = None
CACHE_HITS = 0
ABSENT = {"__absent__": True}
SOT_CACHE = {}

ERRORS = []
REASONS = Counter()
CALLS = 0
LAST_CALL = None
BLOCKED = False
PARTIAL = False
FAILS = 0
STARTED = time.monotonic()


# ---------------- SECURITE / LOGS ----------------

def safe(value):
    text = str(value)
    for secret in (TOKEN, API_KEY, CHAT_ID, ODDS_API_KEY):
        if secret:
            text = text.replace(secret, "[MASQUE]")
    return text


def error(message):
    message = safe(message)[:350]
    print("DIAGNOSTIC :", message, flush=True)
    if message not in ERRORS:
        ERRORS.append(message)


def budget_available(reserve=0):
    cap = MAX_API_CALLS if RUN_CAP is None else min(MAX_API_CALLS, RUN_CAP)
    return (
        not BLOCKED
        and CALLS < cap - reserve
        and time.monotonic() - STARTED < MAX_RUNTIME_SECONDS
    )


# ---------------- TELEGRAM ----------------

TELEGRAM_CHUNK = 4000  # limite Telegram 4096, avec marge


def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    # 6.6.2 : on coupe entre deux blocs (ligne vide = fin d'un match),
    # sous la limite Telegram (4096) ; ligne à ligne si un bloc est trop long.
    chunks = []
    current = ""

    def push_piece(piece):
        nonlocal current
        if len(current) + len(piece) <= TELEGRAM_CHUNK:
            current += piece
            return
        if current.strip():
            chunks.append(current)
        current = ""
        if len(piece) <= TELEGRAM_CHUNK:
            current = piece
            return
        for line in piece.splitlines(keepends=True):
            while len(line) > TELEGRAM_CHUNK:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:TELEGRAM_CHUNK])
                line = line[TELEGRAM_CHUNK:]
            if len(current) + len(line) > TELEGRAM_CHUNK:
                chunks.append(current)
                current = ""
            current += line

    pieces = text.split("\n\n")
    for position, piece in enumerate(pieces):
        push_piece(piece + ("\n\n" if position < len(pieces) - 1 else ""))

    if current.strip():
        chunks.append(current)
    chunks = [c.strip("\n") or c for c in chunks]

    for chunk in chunks:
        send_chunk(chunk)


def send_chunk(chunk, attempts=3):
    """Envoie un morceau avec nouvel essai (réseau, 429, 5xx)."""
    last = "erreur"
    for attempt in range(attempts):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            last = "reseau : " + type(exc).__name__
            time.sleep(3 * (attempt + 1))
            continue

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if response.ok and payload.get("ok"):
            return

        last = (
            f"HTTP {response.status_code} : "
            + safe(payload.get("description", "erreur"))
        )
        if response.status_code == 429 or response.status_code >= 500:
            params = payload.get("parameters")
            pause = number(params.get("retry_after")) if isinstance(params, dict) else None
            time.sleep(min(pause or 5 * (attempt + 1), 60))
            continue
        break  # 400 / 401 / 403 : inutile de réessayer

    raise RuntimeError("Telegram " + last)


def already_sent(today):
    """Évite le double envoi si le workflow est relancé le même jour."""
    if not ONE_SEND_PER_DAY or os.getenv("FORCE_SEND", "").strip() == "1":
        return False
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as file:
            return file.read().strip() == today.isoformat()
    except OSError:
        return False


def mark_sent(today):
    try:
        os.makedirs(os.path.dirname(SENT_FILE), exist_ok=True)
        with open(SENT_FILE, "w", encoding="utf-8") as file:
            file.write(today.isoformat())
    except OSError as exc:
        error(f"Marqueur d'envoi impossible : {type(exc).__name__} {exc}")


# ---------------- API ----------------

def failed(endpoint):
    global FAILS, BLOCKED
    CACHE[endpoint] = None
    FAILS += 1
    if FAILS >= MAX_CONSECUTIVE_FAILS:
        BLOCKED = True
        error("Trop d'echecs d'affilee : analyse arretee.")
    return None


def retry_pause(response):
    try:
        return max(RETRY_PAUSE, int(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return RETRY_PAUSE


def api_get(endpoint):
    global CALLS, LAST_CALL, BLOCKED, FAILS, CACHE_HITS

    if endpoint in CACHE:
        return CACHE[endpoint]

    # 6.6.0 : cache disque (jour / permanent)
    found, cached, etag = disk_lookup(endpoint)
    if found and not is_fresh(endpoint):
        CACHE_HITS += 1
        CACHE[endpoint] = cached
        return cached

    for attempt in range(MAX_RETRIES + 1):
        if not budget_available():
            if found:  # copie un peu ancienne plutôt que rien
                CACHE[endpoint] = cached
                return cached
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        LAST_CALL = time.monotonic()
        CALLS += 1
        print(f"API {CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            if found and etag:
                response = SESSION.get(
                    f"{BASE_URL}/{endpoint}",
                    timeout=TIMEOUT,
                    headers={"If-None-Match": etag},
                )
            else:
                response = SESSION.get(
                    f"{BASE_URL}/{endpoint}",
                    timeout=TIMEOUT,
                )
        except requests.RequestException:
            error(f"{endpoint} : erreur reseau.")
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            return failed(endpoint)

        read_quota(response)

        if response.status_code == 304 and found:
            FAILS = 0
            CACHE[endpoint] = cached
            return cached

        if response.status_code == 429:
            pause = retry_pause(response)
            elapsed = time.monotonic() - STARTED
            if attempt < MAX_RETRIES and elapsed + pause < MAX_RUNTIME_SECONDS:
                error(f"{endpoint} : limite atteinte, pause {pause} s.")
                time.sleep(pause)
                continue
            error(f"{endpoint} : limite de requetes depassee.")
            BLOCKED = True
            return None

        if response.status_code == 401:
            error("Cle API refusee (401).")
            BLOCKED = True
            return None

        if response.status_code == 404:
            # Ressource absente (stats non couvertes, slug inconnu...) :
            # ce n'est PAS une panne -> pas d'échec d'affilée.
            REASONS["Ressource absente (404)"] += 1
            CACHE[endpoint] = None
            disk_store(endpoint, None)
            return None

        if response.status_code >= 500 and attempt < MAX_RETRIES:
            error(f"{endpoint} : HTTP {response.status_code}, nouvel essai.")
            time.sleep(2 * (attempt + 1))
            continue

        if not response.ok:
            error(f"{endpoint} : HTTP {response.status_code}.")
            return failed(endpoint)

        try:
            payload = response.json()
        except ValueError:
            error(f"{endpoint} : JSON invalide.")
            return failed(endpoint)

        if not isinstance(payload, dict) or payload.get("success") is False:
            error(f"{endpoint} : reponse invalide.")
            return failed(endpoint)

        FAILS = 0
        CACHE[endpoint] = payload
        disk_store(endpoint, payload, response.headers.get("ETag") if hasattr(response, "headers") else None)
        return payload

    return None


# ---------------- CACHE DISQUE / QUOTA (6.6.0) ----------------

def paris_today():
    return datetime.now(PARIS).date()


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(temp, path)
    except (OSError, TypeError, ValueError) as exc:
        error(f"Ecriture {path} impossible : {type(exc).__name__}")


def day_cache_path(day):
    return os.path.join(DISK_CACHE_DIR, f"{day.isoformat()}.json")


def load_disk_cache(day=None):
    day = day or paris_today()
    if DISK["day"] == day.isoformat():
        return
    data = read_json(day_cache_path(day), {})
    DISK.update({
        "day": day.isoformat(),
        "data": data.get("data", {}) if isinstance(data.get("data"), dict) else {},
        "etag": data.get("etag", {}) if isinstance(data.get("etag"), dict) else {},
        "dirty": False,
    })
    if not PERM["loaded"]:
        perm = read_json(os.path.join(DISK_CACHE_DIR, "permanent.json"), {})
        PERM.update({"loaded": True, "data": perm, "dirty": False})
    purge_disk_cache(day)


def purge_disk_cache(day):
    """Supprime les caches du jour plus vieux que CACHE_KEEP_DAYS."""
    try:
        names = os.listdir(DISK_CACHE_DIR)
    except OSError:
        return
    for name in names:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", name)
        if not match:
            continue
        try:
            age = (day - datetime.fromisoformat(match.group(1)).date()).days
        except ValueError:
            continue
        if age > CACHE_KEEP_DAYS:
            try:
                os.remove(os.path.join(DISK_CACHE_DIR, name))
            except OSError:
                pass


def save_disk_cache():
    if DISK["day"] and DISK["dirty"]:
        write_json(
            day_cache_path(datetime.fromisoformat(DISK["day"]).date()),
            {"data": DISK["data"], "etag": DISK["etag"]},
        )
        DISK["dirty"] = False
    if PERM["loaded"] and PERM["dirty"]:
        write_json(os.path.join(DISK_CACHE_DIR, "permanent.json"), PERM["data"])
        PERM["dirty"] = False


def is_fresh(endpoint):
    """Données qui bougent dans la journée : liste des matchs d'aujourd'hui
    et d'hier (scores). Redemandées à chaque lancement (avec ETag)."""
    match = re.match(r"^fixtures/date/(\d{4}-\d{2}-\d{2})$", endpoint)
    if not match:
        return False
    try:
        day = datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return True
    return (paris_today() - day).days <= 1


def disk_lookup(endpoint):
    """Renvoie (trouvé, payload, etag)."""
    load_disk_cache()
    if endpoint in PERM["data"]:
        return True, PERM["data"][endpoint], None
    if endpoint in DISK["data"]:
        value = DISK["data"][endpoint]
        payload = None if value == ABSENT else value
        return True, payload, DISK["etag"].get(endpoint)
    return False, None, None


def disk_store(endpoint, payload, etag=None):
    load_disk_cache()
    if payload is not None and PERMANENT_ENDPOINT.match(endpoint):
        # stats / compos / événements : on ne garde que les matchs terminés
        if payload.get("available") is not False:
            PERM["data"][endpoint] = payload
            PERM["dirty"] = True
            return
    DISK["data"][endpoint] = ABSENT if payload is None else payload
    if etag:
        DISK["etag"][endpoint] = etag
    DISK["dirty"] = True
    if len(DISK["data"]) % 25 == 0:
        save_disk_cache()


def header_number(response, name):
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return number(value)


def read_quota(response):
    """Lit le quota restant et resserre le budget de ce lancement."""
    global RUN_CAP
    day_left = header_number(response, "X-RateLimit-Remaining")
    month_left = header_number(response, "X-RateLimit-Month-Remaining")
    if day_left is not None:
        QUOTA["day"] = int(day_left)
    if month_left is not None:
        QUOTA["month"] = int(month_left)
        if not USAGE.get("share_from_api"):
            # 1re lecture du jour : part du jour = quota du mois / jours restants
            now = datetime.now(timezone.utc)
            days_left = month_days_left(now)
            used = int(USAGE.get("calls", 0)) + CALLS
            USAGE["share"] = int((QUOTA["month"] + used) / days_left)
            USAGE["share_from_api"] = True
            RUN_CAP = run_allowance()
    limits = []
    if "day" in QUOTA:
        limits.append(CALLS + QUOTA["day"] - QUOTA_RESERVE)
    if "month" in QUOTA:
        limits.append(CALLS + QUOTA["month"] - QUOTA_RESERVE)
    if limits:
        RUN_CAP = max(0, min([RUN_CAP if RUN_CAP is not None else MAX_API_CALLS] + limits))


def month_days_left(now):
    """Jours restants dans le mois (UTC), aujourd'hui compris."""
    if now.month == 12:
        first_next = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_next = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return max(1, (first_next.date() - now.date()).days)


def usage_path():
    return os.path.join(DISK_CACHE_DIR, "usage.json")


def run_allowance():
    """Appels permis pour CE lancement d'après la part du jour."""
    share = int(USAGE.get("share") or DAILY_BUDGET_DEFAULT)
    before = int(USAGE.get("calls_before_run", 0))
    target = share if before > 0 else int(share * FIRST_RUN_SHARE)
    return max(0, min(MAX_API_CALLS, target - before))


def init_budget(day=None):
    """À appeler au début de main : charge l'usage du jour et fixe RUN_CAP."""
    global RUN_CAP
    day = (day or datetime.now(timezone.utc).date()).isoformat()
    data = read_json(usage_path(), {})
    if data.get("day") != day:
        data = {"day": day, "calls": 0, "runs": 0,
                "odds_calls": 0, "odds_remaining": data.get("odds_remaining")}
    USAGE.clear()
    USAGE.update(data)
    USAGE["calls_before_run"] = int(USAGE.get("calls", 0))
    RUN_CAP = run_allowance()
    print(
        f"BUDGET : {RUN_CAP} appels pour ce lancement "
        f"(deja {USAGE['calls_before_run']} aujourd'hui, part du jour "
        f"{USAGE.get('share') or DAILY_BUDGET_DEFAULT})",
        flush=True,
    )


def save_usage():
    if not USAGE:
        return
    USAGE["calls"] = int(USAGE.get("calls_before_run", 0)) + CALLS
    USAGE["runs"] = int(USAGE.get("runs", 0)) + 1
    data = {k: v for k, v in USAGE.items() if k != "calls_before_run"}
    data.update({"quota_day": QUOTA.get("day"), "quota_month": QUOTA.get("month")})
    write_json(usage_path(), data)


def get_list(payload, key="data"):
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []


# ---------------- CACHE TIRS CADRÉS ----------------

def load_sot_cache():
    if SOT_CACHE:
        return SOT_CACHE

    if os.path.exists(SOT_CACHE_FILE):
        try:
            with open(SOT_CACHE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                SOT_CACHE.update(data)
        except (OSError, ValueError):
            pass

    return SOT_CACHE


def save_sot_cache():
    try:
        os.makedirs(os.path.dirname(SOT_CACHE_FILE), exist_ok=True)
        temp = SOT_CACHE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(SOT_CACHE, file, ensure_ascii=False)
        os.replace(temp, SOT_CACHE_FILE)
    except OSError as exc:
        # Un cache non sauvegardé ne doit pas faire perdre l'analyse
        error(f"Cache tirs cadres non sauvegarde : {type(exc).__name__}")


# ---------------- OUTILS ----------------

def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def kickoff_of(match):
    raw = match.get("kickoff_utc")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PARIS)


def date_of(match):
    kickoff = kickoff_of(match)
    if kickoff is not None:
        return kickoff.date()
    try:
        return datetime.fromisoformat(str(match.get("date"))[:10]).date()
    except ValueError:
        return None


def short_name(value):
    name = " ".join(str(value or "Equipe").split())
    return name if len(name) <= 25 else name[:24] + "…"


def is_youth(fixture):
    return any(
        YOUTH_PATTERN.search(str(fixture.get(field) or ""))
        for field in ("home_team", "away_team")
    )


def league_label(fixture):
    for key in ("league_name", "league", "competition"):
        value = fixture.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            return short_name(value)
    return None


def prioritise(fixtures):
    ordered = sorted(
        fixtures,
        key=lambda f: (
            0 if str(f.get("league_id")) in PRIORITY_LEAGUE_IDS else 1,
            kickoff_of(f),
        ),
    )

    kept = []
    taken = Counter()

    for fixture in ordered:
        league = str(fixture.get("league_id"))
        if (
            league not in PRIORITY_LEAGUE_IDS
            and taken[league] >= MAX_PER_LEAGUE
        ):
            REASONS["Plafond par ligue atteint"] += 1
            continue
        taken[league] += 1
        kept.append(fixture)

    return kept


def boolean(value):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


# ---------------- CLASSEMENT ----------------

def standings_row(league_id, team_id):
    key = str(league_id)
    if key not in STANDINGS_CACHE:
        payload = api_get(f"standings/{league_id}")
        STANDINGS_CACHE[key] = get_list(payload)
        # 6.5.0 : on garde aussi le format / la saison / les groupes
        if isinstance(payload, dict):
            STANDINGS_META[key] = {
                "format": payload.get("format"),
                "season": payload.get("season"),
                "stage": payload.get("stage"),
                "groups": payload.get("groups"),
                "league": payload.get("league"),
            }

    for row in STANDINGS_CACHE[key]:
        if not isinstance(row, dict):
            continue
        team = row.get("team")
        if isinstance(team, dict) and str(team.get("id")) == str(team_id):
            return row
    return None


# ---------------- HISTORIQUE EQUIPE ----------------

def parse_results(matches, team_id, today, slug=None):
    history = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        if str(match.get("status") or "").upper() != "FT":
            continue

        date = date_of(match)
        if date is None:
            continue

        age = (today - date).days
        if age <= 0 or age > MAX_HISTORY_DAYS:
            continue

        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))
        if home_score is None or away_score is None:
            continue
        if home_score < 0 or away_score < 0:
            continue

        is_home = None
        if str(match.get("home_id")) == str(team_id):
            is_home = True
        elif str(match.get("away_id")) == str(team_id):
            is_home = False
        elif slug:
            if match.get("home_slug") == slug:
                is_home = True
            elif match.get("away_slug") == slug:
                is_home = False

        if is_home is None:
            continue

        history.append({
            "id": match.get("id"),
            "date": date,
            "venue": "home" if is_home else "away",
            "gf": home_score if is_home else away_score,
            "ga": away_score if is_home else home_score,
            # 6.4.0 : vrais identifiants du match historique (tirs cadrés)
            "home_id": match.get("home_id"),
            "away_id": match.get("away_id"),
        })

    return history


def merge_history(*groups):
    unique = {}
    for group in groups:
        for match in group:
            identifier = match.get("id")
            if identifier is None:
                identifier = (
                    match["date"], match["venue"],
                    match["gf"], match["ga"],
                )
            unique[identifier] = match

    return sorted(unique.values(), key=lambda m: m["date"], reverse=True)


def history_from_form(row, today):
    history = []
    form = row.get("form") if isinstance(row, dict) else None
    if not isinstance(form, list):
        return history

    for match in form:
        if not isinstance(match, dict):
            continue

        date = date_of(match)
        home = boolean(match.get("is_home"))
        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))

        if None in (date, home, home_score, away_score):
            continue
        if home_score < 0 or away_score < 0:
            continue
        if not 0 < (today - date).days <= MAX_HISTORY_DAYS:
            continue

        history.append({
            "id": None,
            "date": date,
            "venue": "home" if home else "away",
            "gf": home_score if home else away_score,
            "ga": away_score if home else home_score,
            "home_id": None,
            "away_id": None,
        })

    return merge_history(history)


def team_history(team_id, league_id, today):
    key = str(team_id)
    if key in TEAM_CACHE:
        return TEAM_CACHE[key]

    payload = api_get(f"teams/{team_id}")
    history = parse_results(get_list(payload, "matches"), team_id, today)
    history = merge_history(history)

    team = payload.get("team") if isinstance(payload, dict) else {}
    team = team if isinstance(team, dict) else {}
    slug = team.get("slug")
    row = None

    if len(history) < HISTORY_N and not slug:
        row = standings_row(league_id, team_id)
        row_team = row.get("team") if isinstance(row, dict) else {}
        if isinstance(row_team, dict):
            slug = row_team.get("slug")

    if len(history) < HISTORY_N and slug:
        slug_payload = api_get(f"teams/slug/{quote(str(slug), safe='')}")
        extra = parse_results(
            get_list(slug_payload, "results"),
            team_id,
            today,
            slug=str(slug),
        )
        history = merge_history(history, extra)

    if len(history) < MIN_HISTORY:
        if row is None:
            row = standings_row(league_id, team_id)

        fallback = history_from_form(row, today)
        if len(fallback) > len(history):
            history = fallback

    print(
        f"HISTORIQUE FINAL EQUIPE {team_id} : "
        f"{len(history)} resultats exploitables",
        flush=True,
    )

    TEAM_CACHE[key] = history
    return history


# ---------------- MODELE EQUIPE ----------------

def weighted_stats(matches):
    weights = [DECAY ** i for i in range(len(matches))]
    total = sum(weights)

    def average(function):
        return sum(
            weight * function(match)
            for weight, match in zip(weights, matches)
        ) / total

    return {
        "gf": average(lambda m: m["gf"]),
        "ga": average(lambda m: m["ga"]),
        "btts": average(lambda m: float(m["gf"] > 0 and m["ga"] > 0)),
        "over15": average(lambda m: float(m["gf"] + m["ga"] >= 2)),
    }


def profile(history, venue, today):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    overall = weighted_stats(recent)

    local_matches = [
        m for m in history if m["venue"] == venue
    ][:VENUE_N]

    if len(local_matches) >= 3:
        venue_weight = min(0.50, len(local_matches) / (len(local_matches) + 6))
        local = weighted_stats(local_matches)
    else:
        venue_weight = 0
        local = overall

    result = {
        metric: (
            (1 - venue_weight) * overall[metric]
            + venue_weight * local[metric]
        )
        for metric in overall
    }

    n = len(recent)
    for metric, prior in PRIORS.items():
        result[metric] = (
            result[metric] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["n"] = n
    result["venue_n"] = len(local_matches)
    return result


def estimate(home, away):
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)

    # 6.4.0 : l'avantage du terrain est déjà dans les stats domicile /
    # extérieur quand on en a au moins 3 -> le bonus ne s'applique que sinon
    home_adjust = HOME_ADJUST if home.get("venue_n", 0) < 3 else 0.0
    away_adjust = AWAY_ADJUST if away.get("venue_n", 0) < 3 else 0.0
    p_home = min(p_home + home_adjust, HOME_PROB_CAP)
    p_away = min(max(p_away + away_adjust, 0.0), HOME_PROB_CAP)

    total = lambda_home + lambda_away

    btts = (
        0.75 * p_home * p_away
        + 0.25 * (home["btts"] + away["btts"]) / 2
    )

    over15 = (
        0.75 * (1 - math.exp(-total) * (1 + total))
        + 0.25 * (home["over15"] + away["over15"]) / 2
    )

    return lambda_home, lambda_away, {
        "home": p_home,
        "away": p_away,
        "btts": btts,
        "over15": over15,
    }


# ---------------- TICKET (FORME + MARCHÉS BUTS) ----------------

def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def win_probabilities(lambda_home, lambda_away, limit=12):
    home_pmf = [poisson_pmf(i, lambda_home) for i in range(limit)]
    away_pmf = [poisson_pmf(i, lambda_away) for i in range(limit)]
    p_home = sum(
        home_pmf[i] * away_pmf[j]
        for i in range(limit) for j in range(limit) if i > j
    )
    p_away = sum(
        home_pmf[i] * away_pmf[j]
        for i in range(limit) for j in range(limit) if i < j
    )
    return p_home, p_away


def extra_probabilities(lambda_home, lambda_away):
    """Marchés supplémentaires (journalisés, jamais publiés comme pronostics)."""
    total = lambda_home + lambda_away
    p_win_home, p_win_away = win_probabilities(lambda_home, lambda_away)
    return {
        "win_home": p_win_home,
        "win_away": p_win_away,
        "home15": 1 - math.exp(-lambda_home) * (1 + lambda_home),
        "away15": 1 - math.exp(-lambda_away) * (1 + lambda_away),
        "over25": 1 - math.exp(-total) * (1 + total + total ** 2 / 2),
    }


def form_stats(history):
    last = history[:TICKET_FORM_N]
    n = len(last)
    if n < 3:
        return None

    def share(function):
        return sum(1 for m in last if function(m)) / n

    points = sum(
        3 if m["gf"] > m["ga"] else 1 if m["gf"] == m["ga"] else 0
        for m in last
    )
    letters = [
        "V" if m["gf"] > m["ga"] else "N" if m["gf"] == m["ga"] else "D"
        for m in reversed(last)
    ]

    def streak(function):
        count = 0
        for m in last:  # du plus récent au plus ancien
            if not function(m):
                break
            count += 1
        return count

    return {
        "n": n,
        "points": points,
        "pts_rate": points / (3 * n),
        "scored": share(lambda m: m["gf"] > 0),
        "conceded": share(lambda m: m["ga"] > 0),
        "loss_streak": streak(lambda m: m["gf"] < m["ga"]),
        "dry_streak": streak(lambda m: m["gf"] == 0),
        "g2": share(lambda m: m["gf"] >= 2),
        "c2": share(lambda m: m["ga"] >= 2),
        "o15": share(lambda m: m["gf"] + m["ga"] >= 2),
        "o25": share(lambda m: m["gf"] + m["ga"] >= 3),
        "btts": share(lambda m: m["gf"] > 0 and m["ga"] > 0),
        "gf": int(sum(m["gf"] for m in last)),
        "ga": int(sum(m["ga"] for m in last)),
        "letters": " ".join(letters),
    }


def blocked_markets(home_form, away_form):
    """Filtre de forme sur les 5 derniers matchs des deux équipes.

    Renvoie {marché: raison} pour chaque marché interdit. Sans forme
    exploitable (moins de 3 matchs), on interdit tout : pas de pari à
    l'aveugle.
    """
    every = ("home", "away", "btts", "over15", "over25",
             "win_home", "win_away", "home15", "away15")
    if home_form is None or away_form is None:
        return {m: "forme récente insuffisante" for m in every}

    blocked = {}
    sides = (
        ("home", home_form, away_form),
        ("away", away_form, home_form),
    )
    for side, team, opponent in sides:
        n = team["n"]
        scored = round(team["scored"] * n)
        conceded = round(opponent["conceded"] * opponent["n"])
        # seuils ramenés au nombre de matchs dispo (3 sur 5 -> 2 sur 3)
        min_scored = math.ceil(FORM_MIN_SCORED * n / TICKET_FORM_N)
        min_conceded = math.ceil(FORM_MIN_CONCEDED * opponent["n"] / TICKET_FORM_N)

        # Équipe sur une série de défaites : jamais sa victoire sèche
        if team["loss_streak"] >= LOSING_STREAK_MAX:
            blocked[f"win_{side}"] = f"{team['loss_streak']} défaites d'affilée"
        # Adversaire qui ne perd plus : pas de victoire non plus si
        # l'équipe ne gagne pas plus souvent que lui
        elif team["pts_rate"] <= opponent["pts_rate"]:
            blocked[f"win_{side}"] = "forme pas meilleure que l'adversaire"

        if team["dry_streak"] >= DRY_STREAK_MAX:
            reason = f"{team['dry_streak']} matchs sans marquer"
            blocked[side] = reason
            blocked[f"{side}15"] = reason
        elif scored < min_scored:
            blocked[side] = f"a marqué dans {scored}/{n} seulement"
            blocked[f"{side}15"] = blocked[side]
        elif conceded < min_conceded:
            blocked[side] = (
                f"adversaire a encaissé dans {conceded}/{opponent['n']} seulement"
            )
            blocked[f"{side}15"] = blocked[side]

        # +1,5 but équipe : il faut des matchs à 2 buts et une défense poreuse
        if f"{side}15" not in blocked and (team["g2"] + opponent["c2"]) / 2 < FORM_MIN_RATE:
            blocked[f"{side}15"] = "trop peu de matchs à 2 buts marqués/encaissés"

    # BTTS : les deux équipes doivent pouvoir marquer
    if "home" in blocked or "away" in blocked:
        blocked["btts"] = blocked.get("home") or blocked.get("away")
    elif (home_form["btts"] + away_form["btts"]) / 2 < FORM_MIN_RATE:
        blocked["btts"] = "peu de BTTS sur les 5 derniers"

    if (home_form["o15"] + away_form["o15"]) / 2 < FORM_MIN_RATE + 0.2:
        blocked["over15"] = "peu de matchs à 2 buts ou plus"
    if (home_form["o25"] + away_form["o25"]) / 2 < FORM_MIN_RATE:
        blocked["over25"] = "peu de matchs à 3 buts ou plus"

    return blocked


def ticket_candidates(probabilities, extra, home_form, away_form, motivation=None):
    """Un candidat par marché : probabilité modèle + forme des 5 derniers matchs."""
    if home_form is None or away_form is None:
        return []

    prob = {**probabilities, **extra}
    blocked = blocked_markets(home_form, away_form)
    motivation = motivation or {}
    form = {
        # 6.4.0 : "équipe +0,5 but" a enfin un score de forme
        "home": (home_form["scored"] + away_form["conceded"]) / 2,
        "away": (away_form["scored"] + home_form["conceded"]) / 2,
        "win_home": (home_form["pts_rate"] + 1 - away_form["pts_rate"]) / 2,
        "win_away": (away_form["pts_rate"] + 1 - home_form["pts_rate"]) / 2,
        "home15": (home_form["g2"] + away_form["c2"]) / 2,
        "away15": (away_form["g2"] + home_form["c2"]) / 2,
        "over15": (home_form["o15"] + away_form["o15"]) / 2,
        "over25": (home_form["o25"] + away_form["o25"]) / 2,
        "btts": (home_form["btts"] + away_form["btts"]) / 2,
    }

    candidates = []
    for market, form_rate in form.items():
        p = prob.get(market)
        if p is None or market in blocked or market in NOT_PROPOSED:
            continue
        score = (1 - TICKET_FORM_WEIGHT) * p + TICKET_FORM_WEIGHT * form_rate
        score += motivation_adjust(market, motivation)
        score += MARKET_RANK_BONUS.get(market, 0.0)
        candidates.append({
            "market": market,
            "prob": p,
            "form": form_rate,
            "score": score,
        })
    return candidates


def build_ticket(all_results, now):
    """4 jambes (4 matchs différents) : cote juste >= cible, meilleure forme."""
    pool = []

    legs = []
    for result in all_results:
        kickoff = kickoff_of(result["fixture"])
        if kickoff is None or kickoff <= now:
            continue
        for candidate in result.get("candidates", []):
            if TICKET_MIN_PROB <= candidate["prob"] <= TICKET_MAX_PROB:
                pool.append((result, candidate))

    pool.sort(key=lambda item: -item[1]["score"])

    # 6.6.0 : au plus TICKET_PER_MATCH marchés par match dans la sélection
    # (sinon un même match occupe la place de matchs différents)
    per_match = Counter()
    trimmed = []
    for item in pool:
        fid = str(item[0]["fixture"].get("id"))
        if per_match[fid] >= TICKET_PER_MATCH:
            continue
        per_match[fid] += 1
        trimmed.append(item)

    for size in (TICKET_POOL, TICKET_POOL_WIDE):
        legs = best_combo(trimmed[:size])
        if legs and legs[0]["_ok"]:
            break
        if len(trimmed) <= size:
            break
    if not legs:
        return []
    for leg in legs:
        leg.pop("_ok", None)
    legs.sort(key=lambda leg: kickoff_of(leg["result"]["fixture"]))
    return legs


def ticket_fair(legs):
    product = 1.0
    for leg in legs:
        product *= leg["prob"]
    return 1 / product if product > 0 else 0


def best_combo(pool):
    """Meilleure combinaison de TICKET_LEGS matchs différents : la mieux
    notée parmi celles à cote juste >= cible, sinon la mieux notée."""
    best_ok = None
    best_any = None

    for combo in itertools.combinations(pool, TICKET_LEGS):
        fixtures = {str(r["fixture"].get("id")) for r, _ in combo}
        if len(fixtures) < TICKET_LEGS:
            continue

        product = 1.0
        for _, candidate in combo:
            product *= candidate["prob"]
        fair = 1 / product
        mean_score = sum(c["score"] for _, c in combo) / TICKET_LEGS

        if fair >= TICKET_TARGET_ODDS:
            if best_ok is None or mean_score > best_ok[0]:
                best_ok = (mean_score, combo)
        # 6.4.0 : repli sur la combinaison la mieux notée (avant : la plus
        # grosse cote, donc la plus risquée)
        if best_any is None or mean_score > best_any[0]:
            best_any = (mean_score, combo)

    if best_ok is not None:
        combo, ok = best_ok[1], True
    elif best_any is not None:
        combo, ok = best_any[1], False
    else:
        return []

    return [{"result": r, **c, "_ok": ok} for r, c in combo]


def ordinal(rank):
    return "1er" if rank == 1 else f"{rank}e"


def season_is_current(season, today):
    """'2026/27' ou '2026' -> saison en cours ? (inconnu -> True)."""
    years = [int(y) for y in re.findall(r"(?<!\d)(\d{4})(?!\d)", str(season or ""))]
    short = re.match(r"^\s*(\d{4})\s*[/-]\s*(\d{2})\s*$", str(season or ""))
    if not years:
        return True
    if short or len(years) >= 2:
        # saison à cheval : commence en juillet/août
        start = years[0]
        expected = today.year if today.month >= 7 else today.year - 1
        return start >= expected
    year = years[0]
    return year == today.year or (today.month <= 2 and year == today.year - 1)


def standings_ok(fixture, today=None):
    """6.5.0 : le classement lu est-il bien celui d'un vrai championnat
    pour CE match ? Sinon (coupe, sélections, groupes, vieille saison,
    tableau agrégé), on n'affiche ni enjeu ni classement."""
    today = today or datetime.now(PARIS).date()
    league_id = fixture.get("league_id")
    key = str(league_id)
    if league_id is None:
        return False

    names = [str(fixture.get("league_name") or ""), league_label(fixture) or ""]
    if fixture.get("stage"):
        return False
    if any(NON_LEAGUE_PATTERN.search(name) for name in names if name):
        return False

    if key not in STANDINGS_CACHE:
        if not budget_available():
            return False
        try:
            standings_row(league_id, fixture.get("home_id"))
        except Exception as exc:
            error(f"Classement illisible : {type(exc).__name__} {exc}")
            return False

    rows = [r for r in STANDINGS_CACHE.get(key, []) if isinstance(r, dict)]
    if not STANDINGS_MIN_SIZE <= len(rows) <= STANDINGS_MAX_SIZE:
        return False

    meta = STANDINGS_META.get(key) or {}
    fmt = meta.get("format")
    if fmt is not None and str(fmt).lower() != "league":
        return False
    if meta.get("stage"):
        return False
    if isinstance(meta.get("groups"), list) and meta["groups"]:
        return False
    if any(r.get("group") for r in rows):
        return False
    league = meta.get("league")
    if isinstance(league, dict) and NON_LEAGUE_PATTERN.search(str(league.get("name") or "")):
        return False
    if not season_is_current(meta.get("season"), today):
        return False

    for side in ("home_id", "away_id"):
        row = next(
            (r for r in rows if isinstance(r.get("team"), dict)
             and str(r["team"].get("id")) == str(fixture.get(side))),
            None,
        )
        if row is None:
            return False
        played = first_number(row, "played", "games", "matches_played")
        if played is not None and played < STANDINGS_MIN_PLAYED:
            return False
        # Forme du tableau trop ancienne -> tableau d'une autre saison
        form = row.get("form")
        if isinstance(form, list) and form:
            dates = [date_of(m) for m in form if isinstance(m, dict)]
            dates = [d for d in dates if d is not None]
            if dates and (today - max(dates)).days > MAX_STALE_DAYS:
                return False
    return True


def motivation_of(fixture):
    """Enjeu des deux équipes d'après le classement (standings/{ligue}).

    Renvoie {"home": libellé|None, "away": libellé|None}. Libellés :
    "titre/Europe", "maintien", "milieu". Rien si la ligue n'a pas de
    classement (coupe, amical) ou si le budget API est épuisé.
    """
    result = {"home": None, "away": None}
    league_id = fixture.get("league_id")
    if league_id is None:
        return result
    # 6.5.0 : pas d'enjeu si ce n'est pas un vrai championnat
    if not standings_ok(fixture):
        return result

    for side in ("home", "away"):
        if not budget_available() and str(league_id) not in STANDINGS_CACHE:
            return result
        try:
            row = standings_row(league_id, fixture.get(f"{side}_id"))
        except Exception as exc:  # données inattendues : on ignore
            error(f"Classement illisible : {type(exc).__name__} {exc}")
            return result
        if not isinstance(row, dict):
            continue
        rank = first_number(row, "position", "rank", "pos", "place")
        size = len(STANDINGS_CACHE.get(str(league_id), []))
        if rank is None or size < 8:
            continue
        if rank <= TOP_ZONE:
            result[side] = "titre/Europe"
        elif rank > size - RELEGATION_ZONE:
            result[side] = "maintien"
        else:
            result[side] = "milieu"
    return result


def motivation_adjust(market, motivation):
    """Petit bonus si l'équipe concernée joue quelque chose, malus si elle
    est en milieu de tableau face à une équipe sous pression."""
    side = None
    if market in ("home", "home15", "win_home"):
        side, other = "home", "away"
    elif market in ("away", "away15", "win_away"):
        side, other = "away", "home"
    if side is None:
        return 0.0
    mine = motivation.get(side)
    theirs = motivation.get(other)
    if mine in ("titre/Europe", "maintien"):
        return MOTIVATION_BONUS
    if mine == "milieu" and theirs in ("titre/Europe", "maintien"):
        return -MOTIVATION_BONUS
    return 0.0


def motivation_text(motivation):
    labels = {"titre/Europe": "🔥 titre/Europe", "maintien": "🆘 maintien",
              "milieu": "😐 milieu"}
    home = labels.get(motivation.get("home"))
    away = labels.get(motivation.get("away"))
    if not home and not away:
        return ""
    return f"{home or '?'} | {away or '?'}"


def standing_text(fixture):
    """Classement des deux équipes (si l'API le fournit), sinon chaîne vide."""
    league_id = fixture.get("league_id")
    texts = []

    # 6.5.0 : pas de "2e/1er sur 64" tiré d'un tableau de coupe
    if not standings_ok(fixture):
        return ""

    for side in ("home_id", "away_id"):
        if not budget_available():
            return ""
        row = standings_row(league_id, fixture.get(side))
        if not isinstance(row, dict):
            return ""
        rank = first_number(row, "position", "rank", "pos", "place")
        points = first_number(row, "points", "pts")
        if rank is None:
            return ""
        text = ordinal(int(rank))
        if points is not None:
            text += f" ({int(points)} pts)"
        texts.append(text)

    size = len(STANDINGS_CACHE.get(str(league_id), []))
    suffix = f" sur {size}" if size else ""
    return f"{texts[0]} · {texts[1]}{suffix}"


# ---------------- TIRS CADRÉS ----------------

def first_number(row, *keys):
    for key in keys:
        value = number(row.get(key))
        if value is not None:
            return value
    return None


def extract_sot_from_stats(stats, home_id, away_id):
    home_sot = None
    away_sot = None

    if not isinstance(stats, dict):
        return home_sot, away_sot

    # Format 1 : dictionnaire home / away
    if isinstance(stats.get("home"), dict) and isinstance(stats.get("away"), dict):
        home_sot = first_number(
            stats["home"], "shots_on_target", "shotsOnTarget"
        )
        away_sot = first_number(
            stats["away"], "shots_on_target", "shotsOnTarget"
        )

        if home_sot is not None and away_sot is not None:
            return home_sot, away_sot

    # Format 2 : liste statistics
    rows = stats.get("statistics")
    if not isinstance(rows, list):
        rows = stats.get("data")

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            team = row.get("team")
            if isinstance(team, dict):
                team_id = str(team.get("id"))
            else:
                team_id = str(row.get("team_id") or row.get("teamId"))

            value = first_number(
                row, "shots_on_target", "shotsOnTarget", "sot"
            )

            if value is None:
                continue

            if team_id == str(home_id):
                home_sot = value
            elif team_id == str(away_id):
                away_sot = value

        # 6.4.0 : une seule équipe reconnue et exactement deux lignes ->
        # l'autre ligne est forcément l'adversaire
        if (home_sot is None) != (away_sot is None):
            values = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                team = row.get("team")
                tid = str(team.get("id")) if isinstance(team, dict) else str(
                    row.get("team_id") or row.get("teamId")
                )
                value = first_number(row, "shots_on_target", "shotsOnTarget", "sot")
                if value is not None:
                    values.append((tid, value))
            if len(values) == 2:
                if home_sot is None:
                    others = [v for t, v in values if t != str(away_id)]
                    home_sot = others[0] if len(others) == 1 else None
                else:
                    others = [v for t, v in values if t != str(home_id)]
                    away_sot = others[0] if len(others) == 1 else None

    return home_sot, away_sot


def match_sot(fixture):
    """Renvoie (tirs cadrés domicile, tirs cadrés extérieur),
    associés aux identifiants réels des équipes."""
    cache = load_sot_cache()
    fixture_id = str(fixture.get("id"))
    home_id = str(fixture.get("home_id"))
    away_id = str(fixture.get("away_id"))

    if fixture_id in cache:
        value = cache[fixture_id]
        if isinstance(value, list) and len(value) == 2:
            return tuple(value)

    payload = api_get(f"fixtures/{fixture_id}/stats")
    if not isinstance(payload, dict):
        return None

    stats = payload.get("data")
    if not isinstance(stats, dict):
        stats = payload

    home_sot, away_sot = extract_sot_from_stats(stats, home_id, away_id)

    if home_sot is None or away_sot is None:
        return None

    cache[fixture_id] = [home_sot, away_sot]
    save_sot_cache()
    return home_sot, away_sot


def sot_history(history, team_id):
    """Tirs cadrés pour / contre de l'équipe sur ses derniers matchs.
    Renvoie aussi le lieu de chaque valeur (domicile / extérieur)."""
    sot_for = []
    sot_against = []
    venues = []

    for match in history[:SOT_HISTORY_N]:
        match_id = match.get("id")
        if not match_id:
            continue

        # 6.4.0 : vrais identifiants du match historique. Avant, on prenait
        # ceux du match du jour : l'adversaire n'était jamais le bon.
        home_id = match.get("home_id")
        away_id = match.get("away_id")
        if home_id is None and away_id is None:
            home_id = team_id if match["venue"] == "home" else None
            away_id = team_id if match["venue"] == "away" else None

        historical_fixture = {
            "id": match_id,
            "home_id": home_id,
            "away_id": away_id,
        }

        result = match_sot(historical_fixture)
        if result is None:
            continue

        home_sot, away_sot = result

        if match["venue"] == "home":
            sot_for.append(home_sot)
            sot_against.append(away_sot)
        else:
            sot_for.append(away_sot)
            sot_against.append(home_sot)
        venues.append(match["venue"])

    return sot_for, sot_against, venues


def weighted_sot(values):
    if not values:
        return None

    weights = [DECAY ** i for i in range(len(values))]
    total = sum(weights)

    return sum(w * v for w, v in zip(weights, values)) / total


def sot_profile(history, venue, today, team_id):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    sot_for, sot_against, venues = sot_history(recent, team_id)

    if len(sot_for) < 3:
        return None

    overall_for = weighted_sot(sot_for)
    overall_against = weighted_sot(sot_against)

    local_for = []
    local_against = []

    # 6.4.0 : on suit le lieu renvoyé par sot_history (avant, le zip avec
    # "recent" se décalait dès qu'un match sans stats était sauté)
    for place, for_value, against_value in zip(venues, sot_for, sot_against):
        if place == venue:
            local_for.append(for_value)
            local_against.append(against_value)

    if len(local_for) >= 3:
        venue_weight = min(0.45, len(local_for) / (len(local_for) + 6))
        for_value = weighted_sot(local_for)
        against_value = weighted_sot(local_against)
    else:
        venue_weight = 0
        for_value = overall_for
        against_value = overall_against

    n = len(sot_for)

    result = {
        "sot_for": (
            (1 - venue_weight) * overall_for
            + venue_weight * for_value
        ),
        "sot_against": (
            (1 - venue_weight) * overall_against
            + venue_weight * against_value
        ),
    }

    for key, prior in (("sot_for", 3.2), ("sot_against", 3.2)):
        result[key] = (
            result[key] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["sot_n"] = n
    return result


def sot_probability(team, opponent):
    lambda_sot = (team["sot_for"] + opponent["sot_against"]) / 2
    lambda_sot = max(lambda_sot, 0.30)

    # P(X >= 3) pour la ligne "plus de 2,5"
    return 1 - math.exp(-lambda_sot) * (
        1 + lambda_sot + lambda_sot ** 2 / 2
    )


# ---------------- COTE MINIMALE ----------------

def min_odds(probability):
    """Cote minimale à exiger : value bet (marge 3 %) et jamais sous
    MIN_LEG_ODDS."""
    if probability is None or probability <= 0:
        return None
    value = math.ceil(ODDS_MARGIN / probability * 100) / 100
    return max(value, MIN_LEG_ODDS)


def fmt_odds(value):
    return f"{value:.2f}".replace(".", ",")


# ---------------- JOURNAL / CALIBRATION ----------------

OLD_FIELDS = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "model_version", "result",
]


def read_predictions():
    old_backup = PREDICTIONS_FILE.replace(".csv", "_ancien.csv")
    if os.path.exists(old_backup):
        recovered = read_old_format(old_backup)
        if recovered:
            current = read_predictions_file()
            known = {
                (r["date"], r["fixture_id"], r["market"], r["player"])
                for r in current
            }
            current += [
                r for r in recovered
                if (r["date"], r["fixture_id"], r["market"], r["player"])
                not in known
            ]
            write_predictions(current)
            os.replace(old_backup, old_backup + ".repris")
    return read_predictions_file()


def read_old_format(path):
    with open(path, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != OLD_FIELDS:
            return []
        rows = list(reader)
    for row in rows:
        row["prob_base"] = row.get("probability", "")
        row["published"] = 1
    return [{k: r.get(k, "") for k in FIELDS} for r in rows]


def read_predictions_file():
    if not os.path.exists(PREDICTIONS_FILE):
        return []
    with open(PREDICTIONS_FILE, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        header = reader.fieldnames
        rows = list(reader)

    if header == FIELDS:
        return rows

    if header == OLD_FIELDS:
        for row in rows:
            row["prob_base"] = row.get("probability", "")
            row["published"] = 1
        return [{k: r.get(k, "") for k in FIELDS} for r in rows]

    # 6.4.0 : journal 6.3 (ou colonnes connues) -> on complète les colonnes
    # manquantes au lieu de repartir de zéro
    if header and set(header) <= set(FIELDS) and "fixture_id" in header:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in rows]

    # En-tête vraiment inconnu : copie de sécurité datée, jamais écrasée
    stamp = datetime.now(PARIS).strftime("%Y%m%d_%H%M%S")
    backup = PREDICTIONS_FILE.replace(".csv", f"_inconnu_{stamp}.csv")
    os.replace(PREDICTIONS_FILE, backup)
    error(f"Journal : en-tete inconnu, fichier mis de cote ({backup}).")
    return []


def write_predictions(rows):
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)
    # Écriture atomique : un plantage pendant l'écriture ne casse pas le CSV
    temp = PREDICTIONS_FILE + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, PREDICTIONS_FILE)


def outcome(market, home_score, away_score):
    if market == "home":
        return home_score > 0
    if market == "away":
        return away_score > 0
    if market == "btts":
        return home_score > 0 and away_score > 0
    if market == "over15":
        return home_score + away_score >= 2
    if market == "over25":
        return home_score + away_score >= 3
    if market == "win_home":
        return home_score > away_score
    if market == "win_away":
        return away_score > home_score
    if market == "home15":
        return home_score >= 2
    if market == "away15":
        return away_score >= 2
    return None


def settle_previous_predictions(today):
    rows = read_predictions()
    pending = []

    for row in rows:
        if row.get("result") not in ("pending", "", None):
            continue
        try:
            row["_date"] = datetime.fromisoformat(row["date"]).date()
        except (ValueError, KeyError):
            continue
        if row["_date"] < today:
            pending.append(row)

    if not pending:
        for row in rows:
            row.pop("_date", None)
        return

    dates = sorted(
        {
            r["_date"] for r in pending
            if (today - r["_date"]).days <= PENDING_EXPIRY_DAYS + 1
        },
        reverse=True,
    )[:MAX_SETTLE_DATES]
    finished = {}
    dead = set()

    for day in dates:
        payload = api_get(f"fixtures/date/{day.isoformat()}")
        for fixture in get_list(payload):
            if not isinstance(fixture, dict):
                continue
            status = str(fixture.get("status") or "").upper()
            identifier = str(fixture.get("id"))
            if status == "FT":
                home = number(fixture.get("home_score"))
                away = number(fixture.get("away_score"))
                if home is not None and away is not None:
                    finished[identifier] = (home, away)
            elif status in {"POSTPONED", "CANCELLED", "CANCELED",
                            "ABANDONED", "SUSPENDED"}:
                dead.add(identifier)

    for row in pending:
        identifier = str(row.get("fixture_id"))
        age = (today - row["_date"]).days

        if identifier in dead:
            row["result"] = "annule"
            continue

        if identifier not in finished:
            if age > PENDING_EXPIRY_DAYS:
                row["result"] = "expire"
            continue

        home_score, away_score = finished[identifier]
        market = row.get("market")

        if market in ("sot_home", "sot_away"):
            sot = match_sot({
                "id": identifier,
                "home_id": row.get("home_id") or None,
                "away_id": row.get("away_id") or None,
            })

            if sot is None:
                if age > PENDING_EXPIRY_DAYS:
                    row["result"] = "expire"
                continue

            home_sot, away_sot = sot
            value = home_sot if market == "sot_home" else away_sot

            row["result"] = "gagne" if value > SOT_LINE else "perdu"
            continue

        won = outcome(market, home_score, away_score)
        if won is None:
            continue
        row["result"] = "gagne" if won else "perdu"

    for row in rows:
        row.pop("_date", None)
    write_predictions(rows)

    settled = [r for r in rows if r.get("result") in ("gagne", "perdu")]
    if settled:
        wins = sum(r["result"] == "gagne" for r in settled)
        print(f"JOURNAL : {wins}/{len(settled)} gagnes au total.", flush=True)


# ---------------- BILAN (ROI / CALIBRATION) ----------------

MARKET_SHORT = {
    "home": "Dom +0,5", "away": "Ext +0,5", "btts": "BTTS",
    "over15": "+1,5 buts", "over25": "+2,5 buts", "win_home": "Vict. dom",
    "win_away": "Vict. ext", "home15": "Dom +1,5", "away15": "Ext +1,5",
    "sot_home": "TC dom", "sot_away": "TC ext",
}


def parse_odds(value):
    """'1,45' ou '1.45' -> 1.45 ; vide ou absurde -> None."""
    odds = number(str(value or "").strip().replace(",", "."))
    return odds if odds is not None and odds > 1 else None


def journal_summary(rows, today, days=STATS_DAYS):
    """Bilan des lignes réglées (gagne / perdu) sur les `days` derniers jours.

    - par marché, pronostics publiés (1) et ticket (2) séparés : réussite,
      proba moyenne annoncée, ROI pour une mise de 1 à la cote jouée
      (odds_taken) si remplie, sinon à la cote min affichée
    - calibration de TOUTES les lignes journalisées par tranche de 10 %
    - score de Brier (0 = parfait, 0,25 = pile ou face)
    """
    groups = {"publie": {}, "ticket": {}}
    buckets = {}
    brier_sum = 0.0
    brier_n = 0
    real_odds = 0

    for row in rows:
        if row.get("result") not in ("gagne", "perdu"):
            continue
        try:
            day = datetime.fromisoformat(str(row.get("date"))).date()
            probability = float(row.get("probability"))
        except (TypeError, ValueError):
            continue
        if not 0 < probability < 1 or not 0 <= (today - day).days <= days:
            continue

        won = row["result"] == "gagne"
        brier_sum += (probability - won) ** 2
        brier_n += 1

        bucket = min(int(probability * 10), 9) / 10
        stats = buckets.setdefault(bucket, [0, 0, 0.0])
        stats[0] += 1
        stats[1] += won
        stats[2] += probability

        published = str(row.get("published"))
        if published not in ("1", "2"):
            continue

        odds = parse_odds(row.get("odds_taken"))
        if odds is None:
            odds = min_odds(probability)
        else:
            real_odds += 1
        group = groups["publie" if published == "1" else "ticket"]
        entry = group.setdefault(row.get("market"), [0, 0, 0.0, 0.0])
        entry[0] += 1
        entry[1] += won
        entry[2] += (odds - 1) if won else -1
        entry[3] += probability

    def finish(group):
        return {
            m: {"n": n, "wins": w, "hit": w / n, "roi": profit / n,
                "profit": profit, "pred": p / n}
            for m, (n, w, profit, p) in group.items()
        }

    return {
        "markets": finish(groups["publie"]),
        "ticket_markets": finish(groups["ticket"]),
        "calibration": {
            b: {"n": n, "hit": w / n, "pred": p / n}
            for b, (n, w, p) in sorted(buckets.items())
        },
        "brier": brier_sum / brier_n if brier_n else None,
        "n": brier_n,
        "real_odds": real_odds,
    }


def summary_lines(summary):
    """Bloc Telegram : bilan des pronostics publiés sur STATS_DAYS jours."""
    if not summary or not summary.get("markets"):
        return []
    lines = [f"📒 BILAN {STATS_DAYS} JOURS (pronos publiés, mise 1)"]
    total_n = total_w = 0
    total_profit = 0.0
    ordered = sorted(summary["markets"].items(), key=lambda i: -i[1]["n"])
    for market, s in ordered:
        total_n += s["n"]
        total_w += s["wins"]
        total_profit += s["profit"]
        lines.append(
            f"• {MARKET_SHORT.get(market, market)} : {s['wins']}/{s['n']}"
            f" ({s['hit']:.0%}, annoncé {s['pred']:.0%})"
            f" · ROI {s['roi']:+.0%}"
        )
    lines.append(
        f"Total : {total_w}/{total_n} ({total_w / total_n:.0%})"
        f" · ROI {total_profit / total_n:+.0%}"
    )
    if summary.get("brier") is not None:
        lines.append(
            f"Brier : {summary['brier']:.3f} (0,25 = pile ou face)".replace(".", ",")
        )
    if not summary.get("real_odds"):
        lines.append(
            "ℹ️ ROI calculé à la cote min : note tes vraies cotes dans la "
            "colonne odds_taken pour le ROI réel."
        )
    if total_n < 30:
        lines.append(f"⚠️ {total_n} paris seulement : échantillon trop petit.")
    lines.append("")
    return lines


def write_summary_file(summary, today):
    """Écrit data/bilan.csv (un tableau lisible dans Excel / Sheets)."""
    os.makedirs(os.path.dirname(SUMMARY_FILE), exist_ok=True)
    temp = SUMMARY_FILE + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["date_bilan", "type", "cle", "n", "gagnes",
                         "reussite", "proba_annoncee", "roi"])
        for kind in ("markets", "ticket_markets"):
            label = "publie" if kind == "markets" else "ticket"
            for market, s in sorted(summary[kind].items()):
                writer.writerow([today.isoformat(), label, market, s["n"],
                                 s["wins"], f"{s['hit']:.3f}",
                                 f"{s['pred']:.3f}", f"{s['roi']:.3f}"])
        for bucket, s in summary["calibration"].items():
            writer.writerow([today.isoformat(), "calibration",
                             f"{bucket:.1f}-{bucket + 0.1:.1f}", s["n"], "",
                             f"{s['hit']:.3f}", f"{s['pred']:.3f}", ""])
        if summary["brier"] is not None:
            writer.writerow([today.isoformat(), "brier", "", summary["n"],
                             "", "", "", f"{summary['brier']:.4f}"])
    os.replace(temp, SUMMARY_FILE)


def print_summary(summary):
    print("\n===== BILAN JOURNAL =====", flush=True)
    for kind in ("markets", "ticket_markets"):
        for market, s in summary[kind].items():
            print(
                f"{kind} {market}: {s['wins']}/{s['n']} hit={s['hit']:.2f}"
                f" pred={s['pred']:.2f} roi={s['roi']:+.3f}",
                flush=True,
            )
    for bucket, s in summary["calibration"].items():
        gap = s["hit"] - s["pred"]
        print(
            f"tranche {bucket:.0%}-{bucket + 0.1:.0%} : prevu {s['pred']:.2f}"
            f" / reel {s['hit']:.2f} (ecart {gap:+.2f}, n={s['n']})",
            flush=True,
        )
    if summary["brier"] is not None:
        print(f"Brier : {summary['brier']:.4f} sur {summary['n']} lignes", flush=True)


# ---------------- INFOS PAR PICK (6.6.0) ----------------

def norm_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def last_name(value):
    tokens = [t for t in norm_text(value).split() if len(t) > 1]
    return tokens[-1] if tokens else ""


def team_details(history, venue, today):
    """Stats sans appel en plus, à partir de l'historique déjà chargé."""
    if not history:
        return {}
    details = {}
    local = [m for m in history if m["venue"] == venue][:5]
    if len(local) >= 3:
        details["venue_n"] = len(local)
        details["venue_gf"] = sum(m["gf"] for m in local) / len(local)
        details["venue_ga"] = sum(m["ga"] for m in local) / len(local)
    last = history[:10]
    if len(last) >= 5:
        details["n"] = len(last)
        details["clean_sheets"] = sum(m["ga"] == 0 for m in last) / len(last)
        details["failed_to_score"] = sum(m["gf"] == 0 for m in last) / len(last)
    rest = (today - history[0]["date"]).days
    if 0 < rest <= 60:
        details["rest"] = rest
    return details


def market_side(market):
    if market in ("home", "home15", "win_home", "sot_home"):
        return "home"
    if market in ("away", "away15", "win_away", "sot_away"):
        return "away"
    return None


def h2h_info(fixture):
    """5 derniers face-à-face (point de vue du match du jour)."""
    home_id, away_id = fixture.get("home_id"), fixture.get("away_id")
    payload = api_get(f"fixtures/h2h/{home_id}/{away_id}")
    rows = []
    for match in get_list(payload):
        if not isinstance(match, dict):
            continue
        if str(match.get("status") or "").upper() not in ("FT", "AET", "PENS"):
            continue
        hs, as_ = number(match.get("home_score")), number(match.get("away_score"))
        if hs is None or as_ is None:
            continue
        if str(match.get("home_id")) == str(home_id):
            gh, ga = hs, as_
        elif str(match.get("home_id")) == str(away_id):
            gh, ga = as_, hs
        else:
            continue
        rows.append((str(match.get("date") or ""), int(gh), int(ga)))
    rows.sort(reverse=True)
    rows = rows[:H2H_N]
    if len(rows) < 3:
        return None
    return {
        "n": len(rows),
        "home_scored": sum(gh > 0 for _, gh, _ in rows),
        "away_scored": sum(ga > 0 for _, _, ga in rows),
        "over15": sum(gh + ga >= 2 for _, gh, ga in rows),
        "scores": [f"{gh}-{ga}" for _, gh, ga in rows],
    }


def top_scorer(league_id, team_id):
    payload = api_get(f"leagues/{league_id}/topscorers?limit=100")
    best = None
    for row in get_list(payload):
        if not isinstance(row, dict) or str(row.get("team_id")) != str(team_id):
            continue
        goals = number(row.get("goals"))
        if goals is None or goals < TOPSCORER_MIN_GOALS:
            continue
        if best is None or goals > best[1]:
            best = (row.get("player_name"), goals)
    return best


def last_match(history, today):
    """Dernier match de l'équipe avec un identifiant, s'il est récent."""
    for match in history[:1]:
        if match.get("id") and (today - match["date"]).days <= LINEUP_MAX_AGE_DAYS:
            return match
    return None


def lineup_names(match):
    """Noms des joueurs ayant joué le dernier match (titulaires + entrés)."""
    payload = api_get(f"fixtures/{match['id']}/lineups")
    if not isinstance(payload, dict) or payload.get("available") is False:
        return None
    data = payload.get("data")
    side = data.get(match["venue"]) if isinstance(data, dict) else None
    if not isinstance(side, dict):
        return None
    names = set()
    starters = side.get("starters") if isinstance(side.get("starters"), list) else []
    subs = side.get("subs") if isinstance(side.get("subs"), list) else []
    for player in starters:
        if isinstance(player, dict):
            names.add(last_name(player.get("name")))
    for player in subs:
        if isinstance(player, dict) and (number(player.get("mins_played")) or 0) > 0:
            names.add(last_name(player.get("name")))
    names.discard("")
    return names if len(starters) >= 7 else None


def red_cards_last_match(match):
    payload = api_get(f"fixtures/{match['id']}/events")
    players = []
    for event in get_list(payload):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or "").lower()
        if "red" in kind and str(event.get("team_side")) == match["venue"]:
            if event.get("player_name"):
                players.append(str(event["player_name"]))
    return players


def enrich_pick(result, today):
    """Infos + alertes pour un pick publié. Ne renvoie que des données
    réellement lues ; chaque bloc est sauté si le budget manque."""
    fixture = result["fixture"]
    market = shown_picks(result)[0][0] if shown_picks(result) else None
    side = market_side(market)
    info = {"alerts": []}
    home_hist = result.get("home_history") or []
    away_hist = result.get("away_history") or []
    info["home"] = team_details(home_hist, "home", today)
    info["away"] = team_details(away_hist, "away", today)
    names = {"home": short_name(fixture.get("home_team")),
             "away": short_name(fixture.get("away_team"))}

    # Repos court (seulement l'équipe sur laquelle on parie)
    for s in ([side] if side else ["home", "away"]):
        rest = info[s].get("rest")
        if rest is not None and rest <= REST_ALERT_DAYS:
            info["alerts"].append(f"{display_name(names[s])} {rest} j de repos")

    # H2H
    if budget_available():
        h2h = h2h_info(fixture)
        if h2h:
            info["h2h"] = h2h
            if side in ("home", "away") and market in ("home", "away"):
                scored = h2h[f"{side}_scored"]
                if scored <= H2H_ALERT_MAX:
                    info["alerts"].append(
                        f"H2H : {display_name(names[side])} a marqué {scored}/{h2h['n']}"
                    )
            elif market == "over15" and h2h["over15"] <= H2H_ALERT_MAX:
                info["alerts"].append(f"H2H : +1,5 buts {h2h['over15']}/{h2h['n']}")

    # Buteur absent / expulsé au dernier match (indices, à vérifier)
    for s in ([side] if side else []):
        if not budget_available():
            break
        team_id = fixture.get(f"{s}_id")
        history = home_hist if s == "home" else away_hist
        match = last_match(history, today)
        if match is None:
            continue
        scorer = top_scorer(fixture.get("league_id"), team_id)
        if scorer and budget_available():
            played = lineup_names(match)
            if played is not None and last_name(scorer[0]) not in played:
                info["scorer_absent"] = scorer
                info["alerts"].append(
                    f"{player_short(scorer[0])} absent dernier match (à vérifier)"
                )
        if budget_available():
            reds = red_cards_last_match(match)
            if reds:
                info["reds"] = reds
                info["alerts"].append(
                    ", ".join(player_short(p) for p in reds) + " expulsé dernier match"
                )
    return info


def shown_picks(result):
    return (result.get("picks") or [])[:SHOWN_MARKETS]


def fr1(value):
    return f"{value:.1f}".replace(".", ",")


def detail_lines(result, info):
    """Lignes détaillées pour la fiche (fichier), jamais pour Telegram."""
    fixture = result["fixture"]
    lines = []
    for s, label in (("home", "Dom"), ("away", "Ext")):
        d = info.get(s) or {}
        name = short_name(fixture.get(f"{s}_team"))
        parts = []
        if "venue_gf" in d:
            parts.append(
                f"{fr1(d['venue_gf'])} marqués / {fr1(d['venue_ga'])} encaissés "
                f"({d['venue_n']} derniers {'à domicile' if s == 'home' else 'à l’extérieur'})"
            )
        if "clean_sheets" in d:
            parts.append(f"clean sheets {d['clean_sheets']:.0%}")
            parts.append(f"sans marquer {d['failed_to_score']:.0%} ({d['n']} m.)")
        if "rest" in d:
            parts.append(f"repos {d['rest']} j")
        if parts:
            lines.append(f"{label} {name} : " + " · ".join(parts))
    h2h = info.get("h2h")
    if h2h:
        lines.append(
            f"H2H ({h2h['n']}) : {' '.join(h2h['scores'])} · dom a marqué "
            f"{h2h['home_scored']}/{h2h['n']} · ext {h2h['away_scored']}/{h2h['n']}"
        )
    if info.get("scorer_absent"):
        name, goals = info["scorer_absent"]
        lines.append(f"Buteur {name} ({int(goals)} buts) absent de la compo du dernier match (à vérifier)")
    if info.get("reds"):
        lines.append(f"Expulsé(s) au dernier match : {', '.join(info['reds'])} (suspension possible)")
    for market, price in (info.get("odds") or {}).items():
        lines.append(f"Cote marché {market} : {fmt_odds(price)}")
    return lines


# ---------------- COTES RÉELLES (optionnel, 6.6.0) ----------------

ODDS_STATE = {"sports": None, "events": {}}


def odds_get(path, params):
    """Appel The Odds API (jamais sans clé). Renvoie (json, headers)."""
    params = dict(params, apiKey=ODDS_API_KEY)
    try:
        response = requests.get(f"{ODDS_BASE_URL}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        error(f"Cotes : erreur reseau {type(exc).__name__}")
        return None, {}
    headers = getattr(response, "headers", None) or {}
    if not response.ok:
        error(f"Cotes : HTTP {response.status_code}")
        return None, headers
    try:
        return response.json(), headers
    except ValueError:
        return None, headers


def odds_sport_key(fixture):
    name = norm_text(fixture.get("league_name"))
    zone = str(fixture.get("league_zone") or "")
    if not name or NON_LEAGUE_PATTERN.search(name) and "champions league" not in name and "europa league" not in name:
        return None
    for pattern, country, key in ODDS_SPORT_KEYS:
        if pattern in name and (country is None or not zone or country.lower() == zone.lower()):
            return key
    return None


def odds_events(sport_key):
    """Cotes d'une ligue : cache du jour, quota mensuel respecté."""
    if not ODDS_API_KEY:
        return None
    cache_key = f"odds:{sport_key}"
    load_disk_cache()
    if cache_key in DISK["data"]:
        value = DISK["data"][cache_key]
        return None if value == ABSENT else value
    if ODDS_STATE["sports"] is None:
        sports_key = "odds:sports"
        sports = DISK["data"].get(sports_key)
        if not isinstance(sports, list):
            sports, _ = odds_get("sports/", {})  # gratuit (hors quota)
            sports = sports if isinstance(sports, list) else []
            DISK["data"][sports_key] = sports
            DISK["dirty"] = True
        ODDS_STATE["sports"] = {s.get("key") for s in sports if isinstance(s, dict)}
    if sport_key not in ODDS_STATE["sports"]:
        return None
    if int(USAGE.get("odds_calls", 0)) >= ODDS_MAX_CALLS_PER_DAY:
        return None
    remaining = USAGE.get("odds_remaining")
    if remaining is not None and number(remaining) is not None and number(remaining) < ODDS_MIN_REMAINING:
        return None
    data, headers = odds_get(
        f"sports/{sport_key}/odds/",
        {"regions": ODDS_REGIONS, "markets": ODDS_MARKETS, "oddsFormat": "decimal"},
    )
    USAGE["odds_calls"] = int(USAGE.get("odds_calls", 0)) + 1
    try:
        left = headers.get("x-requests-remaining")
    except AttributeError:
        left = None
    if number(left) is not None:
        USAGE["odds_remaining"] = int(number(left))
    DISK["data"][cache_key] = data if isinstance(data, list) else ABSENT
    DISK["dirty"] = True
    return data if isinstance(data, list) else None


def same_team(a, b):
    ta, tb = set(norm_text(a).split()), set(norm_text(b).split())
    ta -= {"fc", "cf", "sc", "afc", "ac", "club", "de", "the"}
    tb -= {"fc", "cf", "sc", "afc", "ac", "club", "de", "the"}
    if not ta or not tb:
        return False
    if ta & tb:
        return True
    # "Lyon" / "Olympique Lyonnais", "Man Utd" / "Manchester United"
    return any(
        len(min(x, y, key=len)) >= 4 and (x.startswith(y) or y.startswith(x))
        for x in ta for y in tb
    )


def find_event(events, fixture):
    kickoff = kickoff_of(fixture)
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if not (same_team(event.get("home_team"), fixture.get("home_team"))
                and same_team(event.get("away_team"), fixture.get("away_team"))):
            continue
        start = kickoff_of({"kickoff_utc": event.get("commence_time")})
        if kickoff and start and abs((start - kickoff).total_seconds()) > 3 * 3600:
            continue
        return event
    return None


def market_price(event, market):
    """Cote médiane des bookmakers pour un marché du bot, si elle existe."""
    prices = []
    for book in event.get("bookmakers") or []:
        for item in (book.get("markets") or []) if isinstance(book, dict) else []:
            key = item.get("key")
            for outcome in item.get("outcomes") or []:
                price = number(outcome.get("price"))
                if price is None or price <= 1:
                    continue
                name = outcome.get("name")
                point = number(outcome.get("point"))
                if key == "totals" and name == "Over":
                    if (market == "over15" and point == 1.5) or (market == "over25" and point == 2.5):
                        prices.append(price)
                elif key == "h2h":
                    if market == "win_home" and name == event.get("home_team"):
                        prices.append(price)
                    elif market == "win_away" and name == event.get("away_team"):
                        prices.append(price)
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]


def real_odds_for(fixture, markets):
    """Vraies cotes {marché: cote} d'un match ({} si pas de clé / marché)."""
    if not ODDS_API_KEY or not markets:
        return {}
    key = odds_sport_key(fixture)
    if not key:
        return {}
    event = find_event(odds_events(key), fixture)
    if not event:
        return {}
    prices = {}
    for market in markets:
        price = market_price(event, market)
        if price:
            prices[market] = price
    return prices


def real_odds(result):
    """Vraies cotes des marchés affichés d'un pick ({} si aucune)."""
    return real_odds_for(result["fixture"], [m for m, _ in shown_picks(result)])


# ---------------- ANALYSE ----------------

def analyse(fixture, today):
    home_history = team_history(
        fixture["home_id"], fixture["league_id"], today
    )
    away_history = team_history(
        fixture["away_id"], fixture["league_id"], today
    )

    home = profile(home_history, "home", today)
    away = profile(away_history, "away", today)

    if home is None or away is None:
        return None

    lambda_home, lambda_away, probabilities = estimate(home, away)

    home_form = form_stats(home_history)
    away_form = form_stats(away_history)
    blocked = blocked_markets(home_form, away_form)

    # 6.4.0 : la forme récente prime sur le modèle
    eligible_picks = []
    for market, probability in probabilities.items():
        if not MIN_PROB <= probability <= MAX_PROB:
            continue
        if market in NOT_PROPOSED:
            continue
        if market in blocked:
            REASONS[f"Forme : {market} écarté"] += 1
            print(f"FORME : {market} écarté ({blocked[market]})", flush=True)
            continue
        eligible_picks.append((market, probability))

    # 6.5.0 : bonus de classement (Domicile +0,5 d'abord à proba proche)
    picks = sorted(
        eligible_picks,
        key=lambda item: item[1] + MARKET_RANK_BONUS.get(item[0], 0.0),
        reverse=True,
    )[:MAX_MARKETS]

    # Tirs cadrés (non publiés) : seulement s'il reste assez de budget API
    home_sot = away_sot = None
    run_cap = MAX_API_CALLS if RUN_CAP is None else min(MAX_API_CALLS, RUN_CAP)
    if CALLS < run_cap * SOT_BUDGET_SHARE:
        home_sot = sot_profile(home_history, "home", today, fixture["home_id"])
        away_sot = sot_profile(away_history, "away", today, fixture["away_id"])
    else:
        REASONS["Tirs cadrés sautés (budget)"] += 1

    # Enjeu : uniquement si un pronostic ou le ticket peut en avoir besoin
    motivation = {"home": None, "away": None}
    if picks or any(TICKET_MIN_PROB <= p <= TICKET_MAX_PROB
                    for p in probabilities.values()):
        motivation = motivation_of(fixture)

    sot_picks = []

    if home_sot and away_sot:
        home_sot_prob = sot_probability(home_sot, away_sot)
        away_sot_prob = sot_probability(away_sot, home_sot)

        if SOT_MIN_PROB <= home_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_home", home_sot_prob))

        if SOT_MIN_PROB <= away_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_away", away_sot_prob))

    # Tirs cadrés : enregistrés dans le journal (write_journal) mais non
    # publiés tant que la calibration n'est pas validée.

    extra = extra_probabilities(lambda_home, lambda_away)
    candidates = ticket_candidates(
        probabilities, extra, home_form, away_form, motivation
    )

    print(
        "MODELE :",
        fixture.get("home_team"), "-", fixture.get("away_team"),
        "probabilites :", probabilities,
        "tirs cadrés :",
        {
            "home": home_sot,
            "away": away_sot,
        },
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "probabilities": probabilities,
        "base": probabilities,
        "best": (
            max(p + MARKET_RANK_BONUS.get(m, 0.0) for m, p in picks)
            if picks else 0
        ),
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "home_sot": home_sot,
        "away_sot": away_sot,
        "extra": extra,
        "home_form": home_form,
        "away_form": away_form,
        "candidates": candidates,
        "blocked": blocked,
        "motivation": motivation,
        "home_history": home_history,
        "away_history": away_history,
    }


# ---------------- MESSAGE ----------------

def market_labels(fixture):
    home = short_name(fixture.get("home_team"))
    away = short_name(fixture.get("away_team"))

    return {
        "home": f"{home} +0,5 but",
        "away": f"{away} +0,5 but",
        "btts": "BTTS Oui",
        "over15": "Plus de 1,5 but",
        "over25": "Plus de 2,5 buts",
        "win_home": f"{home} gagne",
        "win_away": f"{away} gagne",
        "home15": f"{home} +1,5 but",
        "away15": f"{away} +1,5 but",
        "sot_home": f"{home} : plus de 2,5 tirs cadrés",
        "sot_away": f"{away} : plus de 2,5 tirs cadrés",
    }


def ticket_lines(ticket):
    lines = ["🎟️ TICKET DU JOUR (expérimental)"]

    if not ticket:
        lines.extend([
            "Pas de ticket aujourd'hui : pas assez de matchs exploitables.",
            "",
        ])
        return lines

    fair_total = 1.0
    min_total = 1.0

    for index, leg in enumerate(ticket, 1):
        result = leg["result"]
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        kickoff = kickoff_of(fixture).strftime("%H:%M")
        probability = leg["prob"]
        label = market_labels(fixture)[leg["market"]]

        fair_total *= 1 / probability
        min_total *= min_odds(probability)

        lines.append(f"{index}. {home} — {away} · {kickoff}")
        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")
        lines.append(
            f"• {label} : {probability:.0%}"
            f" · cote juste {fmt_odds(1 / probability)}"
        )

        home_form = result.get("home_form")
        away_form = result.get("away_form")
        if home_form and away_form:
            lines.append(
                f"📊 {home} : {home_form['letters']}"
                f" ({home_form['points']} pts, {home_form['gf']} buts pour"
                f" / {home_form['ga']} contre)"
            )
            lines.append(
                f"📊 {away} : {away_form['letters']}"
                f" ({away_form['points']} pts, {away_form['gf']} buts pour"
                f" / {away_form['ga']} contre)"
            )

        if leg.get("standing"):
            lines.append(f"📍 Classement : {leg['standing']}")
        enjeu = motivation_text(result.get("motivation") or {})
        if enjeu:
            lines.append(f"⚔️ Enjeu : {enjeu}")

        lines.append("")

    if fair_total < TICKET_TARGET_ODDS:
        lines.append(
            f"ℹ️ Cote cible {fmt_odds(TICKET_TARGET_ODDS)} non atteinte :"
            " ticket le mieux noté proposé."
        )

    lines.extend([
        f"🎯 Cote juste du ticket : {fmt_odds(fair_total)}"
        f" (environ 1 chance sur {fair_total:.0f})",
        f"💰 Ton bookmaker doit payer au moins {fmt_odds(min_total)} au total.",
        "⚠️ Petite mise seulement. Probabilités de 50 à 72 % pas encore "
        "validées. Blessures, météo et compositions ne sont pas dans les "
        "données du bot : vérifie-les avant de jouer.",
        "",
    ])
    return lines


def build_message(selected, partial, analysed, total, ticket=None, summary=None):
    lines = [
        "⚽ PRONOSTICS DU JOUR",
        datetime.now(PARIS).strftime("📅 %d/%m/%Y"),
        "",
    ]

    selected = sorted(
        selected, key=lambda r: (kickoff_of(r["fixture"]), -r["best"])
    )
    current_hour = None

    for index, result in enumerate(selected, 1):
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        start = kickoff_of(fixture)
        kickoff = start.strftime("%H:%M")

        if start.hour != current_hour:
            current_hour = start.hour
            lines.append(f"⏰ {current_hour:02d}h")

        lines.append(f"{index}. {home} — {away} · {kickoff}")

        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")

        labels = market_labels(fixture)

        for market, probability in result["picks"]:
            lines.append(
                f"• {labels[market]} : {probability:.0%}"
                f" · 💰 min {fmt_odds(min_odds(probability))}"
            )

        home_form = result.get("home_form")
        away_form = result.get("away_form")
        if home_form and away_form:
            lines.append(
                f"📊 Forme : {home_form['letters']} | {away_form['letters']}"
            )
        enjeu = motivation_text(result.get("motivation") or {})
        if enjeu:
            lines.append(f"⚔️ Enjeu : {enjeu}")

        projection = (
            f"{result['lambda_home']:.2f} — {result['lambda_away']:.2f}"
        ).replace(".", ",")

        lines.extend([
            f"⚽ Buts projetés : {projection}",
            "",
        ])

    if not selected:
        lines.extend([
            "Aucun marché estimé dans les filtres actuels.",
            "",
        ])

    lines.extend(ticket_lines(ticket))
    lines.extend(summary_lines(summary))

    lines.append(f"📈 {analysed}/{total} matchs analysés")

    if partial:
        lines.append("⚠️ Analyse partielle.")

    lines.append(
        "💰 Ne joue que si la cote du bookmaker est au moins égale "
        "à la cote min : en dessous, le pari perd sur la durée."
    )
    lines.append("ℹ️ Heures de Paris. Sans garantie.")

    return "\n".join(lines)


# 6.6.1 : noms officiels de fédérations -> nom court
NATION_NAMES = {
    "football union of russia": "Russia",
    "bosnia-herzegovina": "Bosnia",
    "bosnia and herzegovina": "Bosnia",
    "bosnia & herzegovina": "Bosnia",
    "republic of ireland": "Ireland",
    "korea republic": "South Korea",
    "korea dpr": "North Korea",
    "ir iran": "Iran",
    "china pr": "China",
    "united states": "USA",
    "cape verde islands": "Cape Verde",
    "trinidad and tobago": "Trinidad",
    "antigua and barbuda": "Antigua",
    "st. kitts and nevis": "St Kitts",
    "saint kitts and nevis": "St Kitts",
    "st. vincent / grenadines": "St Vincent",
    "st. vincent and the grenadines": "St Vincent",
    "chinese taipei": "Taiwan",
    "north macedonia": "Macedonia",
    "central african republic": "Centrafrique",
    "dr congo": "RD Congo",
    "congo dr": "RD Congo",
}
CLUB_SUFFIXES = {"town", "united", "city", "fc", "afc", "cf"}


def display_name(value):
    """Nom affiché sur Telegram (fédérations raccourcies, sinon inchangé)."""
    name = " ".join(str(value or "Equipe").split())
    name = NATION_NAMES.get(name.lower(), name)
    return short_name(name)


def compact_name(value):
    """Nom encore plus court pour les libellés "Dom-Ext" : sans Town,
    United, City, FC... (gardé si le nom deviendrait vide)."""
    name = display_name(value)
    words = name.split()
    while len(words) > 1 and words[-1].lower().strip(".") in CLUB_SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0].lower().strip(".") in ("fc", "afc", "cf"):
        words.pop(0)
    return " ".join(words)


def player_short(value):
    """'J. Smith' -> 'Smith' (initiales retirées, nom gardé)."""
    words = str(value or "").split()
    kept = [w for w in words if not (len(w) <= 3 and w.endswith("."))]
    return " ".join(kept or words)


LIGHT_LABELS = {
    "home": "{home} +0,5", "away": "{away} +0,5",
    "over15": "{mhome}-{maway} +1,5", "over25": "{mhome}-{maway} +2,5",
    "home15": "{home} +1,5 but", "away15": "{away} +1,5 but",
    "win_home": "{home} gagne", "win_away": "{away} gagne",
    "btts": "BTTS {mhome}-{maway}",
}
TICKET_LABELS = dict(LIGHT_LABELS, home15="{home} +1,5", away15="{away} +1,5")


def light_label(fixture, market, labels=LIGHT_LABELS):
    template = labels.get(market, market)
    return template.format(
        home=display_name(fixture.get("home_team")),
        away=display_name(fixture.get("away_team")),
        mhome=compact_name(fixture.get("home_team")),
        maway=compact_name(fixture.get("away_team")),
    )


def odds_text(probability, price):
    """'cote 1,36 ✅ value' (vraie cote) ou 'cote ~1,25' (estimation bot)."""
    floor = min_odds(probability)
    if price:
        text = f"cote {fmt_odds(price)}"
        if floor is not None and price > floor:
            text += " ✅ value"
        return text
    return f"cote ~{fmt_odds(floor)}" if floor is not None else ""


def fmt_roi(value):
    pct = round(value * 100)
    return "0%" if pct == 0 else f"{pct:+d}%"


def fmt_short(value):
    return fmt_odds(value).rstrip("0").rstrip(",")


def tg_labels(fixture):
    """Libellés des marchés pour Telegram (noms de sélections raccourcis)."""
    named = dict(fixture)
    named["home_team"] = display_name(fixture.get("home_team"))
    named["away_team"] = display_name(fixture.get("away_team"))
    return market_labels(named)


def tg_odds(probability, price):
    """'💰 cote 1,40 ✅ value' si vraie cote, sinon '💰 min 1,25'."""
    if price:
        text = f"💰 cote {fmt_odds(price)}"
        floor = min_odds(probability)
        if floor is not None and price > floor:
            text += " ✅ value"
        return text
    return f"💰 min {fmt_odds(min_odds(probability))}"


def match_head(fixture):
    start = kickoff_of(fixture)
    head = (
        f"{display_name(fixture.get('home_team'))} — "
        f"{display_name(fixture.get('away_team'))} · {start.strftime('%H:%M')}"
    )
    league = league_label(fixture)
    return start, head, (f"🏆 {league}" if league else None)


def build_light_message(selected, partial, ticket=None, summary=None, enrich=None, today=None):
    """Message Telegram (6.6.2) : présentation par heure et par match,
    sans forme / enjeu. Les blocs sont séparés par une ligne vide, ce qui
    permet de couper le message entre deux matchs."""
    enrich = enrich or {}
    day = today or datetime.now(PARIS).date()
    blocks = [f"⚽ PRONOSTICS DU JOUR\n📅 {day.strftime('%d/%m/%Y')}"]

    ordered = sorted(selected, key=lambda r: (kickoff_of(r["fixture"]), -r["best"]))
    current_hour = None
    index = 0
    for result in ordered:
        picks = shown_picks(result)
        if not picks:
            continue
        fixture = result["fixture"]
        info = enrich.get(str(fixture.get("id"))) or {}
        prices = info.get("odds") if isinstance(info.get("odds"), dict) else {}
        start, head, league = match_head(fixture)
        index += 1
        lines = []
        if start.hour != current_hour:
            current_hour = start.hour
            lines.append(f"⏰ {current_hour:02d}h")
        lines.append(f"{index}. {head}")
        if league:
            lines.append(league)
        labels = tg_labels(fixture)
        for market, probability in picks:
            lines.append(
                f"• {labels.get(market, market)} : {probability:.0%}"
                f" · {tg_odds(probability, prices.get(market))}"
            )
        if result.get("lambda_home") is not None and result.get("lambda_away") is not None:
            lines.append(
                f"⚽ Buts projetés : {result['lambda_home']:.2f} — "
                f"{result['lambda_away']:.2f}".replace(".", ",")
            )
        alerts = info.get("alerts") or []
        if alerts:
            lines.append("⚠️ " + " · ".join(alerts))
        blocks.append("\n".join(lines))
    if index == 0:
        blocks.append("Aucun prono solide aujourd'hui.")

    if ticket:
        ticket_odds = enrich.get("__ticket_odds__") or {}
        for number_, leg in enumerate(ticket, 1):
            fixture = leg["result"]["fixture"]
            _, head, league = match_head(fixture)
            probability = leg["prob"]
            price = ticket_odds.get((str(fixture.get("id")), leg["market"]))
            if price:
                odds = f"cote {fmt_odds(price)}"
                floor = min_odds(probability)
                if floor is not None and price > floor:
                    odds += " ✅ value"
            else:
                odds = f"cote juste {fmt_odds(1 / probability)}"
            lines = (["🎟️ TICKET DU JOUR"] if number_ == 1 else []) + [f"{number_}. {head}"]
            if league:
                lines.append(league)
            lines.append(
                f"• {tg_labels(fixture).get(leg['market'], leg['market'])} :"
                f" {probability:.0%} · {odds}"
            )
            blocks.append("\n".join(lines))
        fair = ticket_fair(ticket)
        if fair >= TICKET_TARGET_ODDS:
            blocks.append(f"💰 Cote totale : {fmt_short(fair)}")
        else:
            blocks.append(
                f"💰 Cote {TICKET_TARGET_ODDS:g} non atteinte"
                f" (meilleur {fmt_short(fair)})"
            )
    else:
        blocks.append("🎟️ TICKET DU JOUR\nPas assez de matchs exploitables.")

    footer = []
    if summary and summary.get("markets"):
        n = sum(s["n"] for s in summary["markets"].values())
        wins = sum(s["wins"] for s in summary["markets"].values())
        profit = sum(s["profit"] for s in summary["markets"].values())
        if n:
            footer.append(f"📊 {STATS_DAYS}j : {wins / n:.0%} · ROI {fmt_roi(profit / n)}")
    if partial:
        footer.append("⚠️ Analyse partielle (quota API)")
    if footer:
        blocks.append("\n".join(footer))
    return "\n\n".join(blocks)


def write_fiches(selected, enrich, ticket, partial, analysed, total, summary, now):
    """Fiche complète (ancien message détaillé + infos par pick) dans
    data/fiches/ : tout le détail, sans alourdir Telegram."""
    try:
        text = build_message(selected, partial, analysed, total, ticket, summary)
        extra = ["", "===== DÉTAIL PAR PICK ====="]
        for result in sorted(selected, key=lambda r: kickoff_of(r["fixture"])):
            fixture = result["fixture"]
            info = enrich.get(str(fixture.get("id"))) or {}
            extra.append(
                f"{short_name(fixture.get('home_team'))} — "
                f"{short_name(fixture.get('away_team'))} · "
                f"{kickoff_of(fixture).strftime('%H:%M')}"
            )
            extra.extend("  " + line for line in detail_lines(result, info))
            extra.extend("  ⚠️ " + a for a in info.get("alerts") or [])
            extra.append("")
        extra.append(f"Appels API ce lancement : {CALLS} · cache : {CACHE_HITS}")
        os.makedirs(FICHES_DIR, exist_ok=True)
        path = os.path.join(FICHES_DIR, now.strftime("%Y-%m-%d_%H%M") + ".txt")
        with open(path, "w", encoding="utf-8") as file:
            file.write(text + "\n" + "\n".join(extra) + "\n")
        return path
    except Exception as exc:
        error(f"Fiche impossible : {type(exc).__name__} {exc}")
        return None


def write_journal(all_results, selected, today, ticket=()):
    rows = read_predictions()
    known = {
        (r["date"], r["fixture_id"], r["market"], r["player"])
        for r in rows
    }
    published = {
        (str(r["fixture"].get("id")), m)
        for r in selected for m, _ in shown_picks(r)
    }

    ticket_keys = {
        (str(leg["result"]["fixture"].get("id")), leg["market"])
        for leg in ticket
    }

    for result in all_results:
        fixture = result["fixture"]
        fid = str(fixture.get("id"))
        base_info = {
            "date": today.isoformat(),
            "fixture_id": fid,
            "match": f"{fixture.get('home_team')} - {fixture.get('away_team')}",
            "league": fixture.get("league_name") or "",
            "kickoff": kickoff_of(fixture).strftime("%H:%M"),
            "model_version": MODEL_VERSION,
            "result": "pending",
            "home_id": str(fixture.get("home_id") or ""),
            "away_id": str(fixture.get("away_id") or ""),
            "odds_taken": "",
        }

        all_markets = dict(result["probabilities"])
        all_markets.update(result.get("extra") or {})

        if result.get("home_sot") and result.get("away_sot"):
            all_markets["sot_home"] = sot_probability(
                result["home_sot"], result["away_sot"]
            )
            all_markets["sot_away"] = sot_probability(
                result["away_sot"], result["home_sot"]
            )

        for market, probability in all_markets.items():
            if probability < LOG_MIN_PROB:
                continue

            key = (base_info["date"], fid, market, "")
            if key in known:
                continue

            rows.append({
                **base_info,
                "market": market,
                "player": "",
                "probability": f"{probability:.4f}",
                "prob_base": f"{probability:.4f}",
                "published": (
                    1 if (fid, market) in published
                    else 2 if (fid, market) in ticket_keys
                    else 0
                ),
            })
            known.add(key)

    write_predictions(rows)
    print(f"JOURNAL : {len(rows)} lignes au total.", flush=True)


# ---------------- EXECUTION ----------------

def main():
    global PARTIAL

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")
    if not API_KEY:
        send_message("❌ Secret API_KEY absent.")
        return

    now = datetime.now(PARIS)
    today = now.date()

    # 6.6.0 : cache disque + budget du jour
    load_disk_cache(today)
    init_budget()
    try:
        run(now, today)
    finally:
        save_disk_cache()
        save_usage()
        print(f"APPELS API : {CALLS} (cache : {CACHE_HITS}) · quota {QUOTA}", flush=True)


def run(now, today):
    global PARTIAL

    try:
        settle_previous_predictions(today)
    except Exception as exc:
        error(f"Verification du journal impossible : {type(exc).__name__} {exc}")

    if already_sent(today):
        print("Message deja envoye aujourd'hui (FORCE_SEND=1 pour forcer).", flush=True)
        return

    payload = api_get(f"fixtures/date/{today.isoformat()}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        send_message("⚠️ Analyse indisponible : matchs SportAPI illisibles.")
        return

    fixtures = payload["data"]
    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue

        kickoff = kickoff_of(fixture)
        if kickoff is None:
            continue

        if kickoff.date() != today or kickoff <= now:
            continue

        if str(fixture.get("status") or "").casefold() != "scheduled":
            continue

        if any(
            fixture.get(field) is None
            for field in ("home_id", "away_id", "league_id")
        ):
            continue

        if EXCLUDE_YOUTH and is_youth(fixture):
            continue

        identifier = fixture.get("id") or (
            fixture["home_id"],
            fixture["away_id"],
            str(fixture.get("kickoff_utc")),
        )

        if identifier in seen:
            continue

        seen.add(identifier)
        eligible.append(fixture)

    eligible = prioritise(eligible)

    results = []
    all_results = []
    analysed = 0

    for fixture in eligible:
        # 6.6.0 : on garde ENRICH_RESERVE appels pour les infos des picks
        if not budget_available(ENRICH_RESERVE):
            PARTIAL = True
            break

        if kickoff_of(fixture) <= datetime.now(PARIS):
            continue

        try:
            result = analyse(fixture, today)
        except Exception as exc:
            error(
                f"Lecture historique impossible ({fixture.get('id')}) : "
                f"{type(exc).__name__} {exc}"
            )
            continue

        if result is None:
            REASONS["Historique insuffisant ou trop ancien"] += 1
            continue

        analysed += 1
        all_results.append(result)

        if result["picks"]:
            results.append(result)

    if BLOCKED:
        PARTIAL = True

    results.sort(
        key=lambda r: (-r["best"], kickoff_of(r["fixture"]))
    )

    publication_time = datetime.now(PARIS)
    selected = [
        r for r in results
        if r["picks"] and kickoff_of(r["fixture"]) > publication_time
    ][:MAX_MATCHES]

    ticket = []
    try:
        ticket = build_ticket(all_results, publication_time)
        for leg in ticket:
            try:
                leg["standing"] = standing_text(leg["result"]["fixture"])
            except Exception:
                leg["standing"] = ""
    except Exception as exc:
        ticket = []
        error(f"Ticket impossible : {type(exc).__name__} {exc}")

    # 6.6.0 : infos par pick publié (H2H, buteur, cartons, cotes)
    enrich = {}
    for result in selected:
        try:
            info = enrich_pick(result, today)
            prices = real_odds(result)
            if prices:
                info["odds"] = prices
            enrich[str(result["fixture"].get("id"))] = info
        except Exception as exc:
            error(f"Infos pick impossibles : {type(exc).__name__} {exc}")

    ticket_odds = {}
    for leg in ticket or []:
        try:
            fixture = leg["result"]["fixture"]
            price = real_odds_for(fixture, [leg["market"]]).get(leg["market"])
            if price:
                ticket_odds[(str(fixture.get("id")), leg["market"])] = price
        except Exception as exc:
            error(f"Cote ticket impossible : {type(exc).__name__} {exc}")
    enrich["__ticket_odds__"] = ticket_odds

    try:
        write_journal(all_results, selected, today, ticket)
    except Exception as exc:
        error(f"Ecriture du journal impossible : {type(exc).__name__} {exc}")

    summary = None
    try:
        summary = journal_summary(read_predictions_file(), today)
        print_summary(summary)
        write_summary_file(summary, today)
    except Exception as exc:
        error(f"Bilan du journal impossible : {type(exc).__name__} {exc}")

    print("\n===== BILAN FINAL =====", flush=True)
    print("MATCHS RECUS :", len(fixtures), flush=True)
    print("MATCHS A VENIR :", len(eligible), flush=True)
    print("MATCHS ANALYSABLES :", analysed, flush=True)
    print("MATCHS PUBLIES :", len(selected), flush=True)
    print("REQUETES :", CALLS, flush=True)
    print("ANALYSE PARTIELLE :", PARTIAL, flush=True)
    print("DIAGNOSTIC :", dict(REASONS), flush=True)
    print("ERREURS :", ERRORS, flush=True)

    fiche = write_fiches(
        selected, enrich, ticket, PARTIAL, analysed, len(eligible), summary, now
    )
    if fiche:
        print("FICHE :", fiche, flush=True)

    send_message(build_light_message(selected, PARTIAL, ticket, summary, enrich, today))
    mark_sent(today)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERREUR FATALE :", type(exc).__name__, safe(exc), flush=True)
        raise SystemExit(1)
