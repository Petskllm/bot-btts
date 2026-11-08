"""
TEST DE COUVERTURE DES STATS DE TIRS — SportAPI

Question : est-ce que /fixtures/{id}/stats renvoie des tirs, et pour
quelles ligues ?

Coût : environ 50 requêtes (3 jours de matchs + ~45 stats de match).
Lancement : manuel (workflow "Test stats de tirs"), jamais automatique.
Ne modifie ni le bot ni le journal.

Résultat : rapport dans les logs GitHub + résumé sur Telegram.
"""

import math
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

DAYS = [1, 2, 3]            # jours passés utilisés pour trouver des matchs
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "45"))
PER_LEAGUE = 3              # matchs testés par ligue (pour varier les ligues)

MAX_CALLS = 70
MIN_INTERVAL = 0.4
TIMEOUT = 25
MAX_RUNTIME_SECONDS = 600

YOUTH_PATTERN = re.compile(
    r"\bU[- ]?(1[5-9]|2[0-3])\b|\byouth\b|\bjuniors?\b"
    r"|\bprimavera\b|\breserves?\b|\bamateurs?\b|\bII$",
    re.IGNORECASE,
)

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-shots-check/1.0",
})

STARTED = time.monotonic()
CALLS = 0
LAST_CALL = None
FIRST_RAW_SHOWN = False


def safe(value):
    text = str(value)
    for secret in (TOKEN, API_KEY, CHAT_ID):
        if secret:
            text = text.replace(secret, "[MASQUE]")
    return text


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram non configure : resume non envoye.", flush=True)
        return
    for start in range(0, len(text), 3000):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text[start:start + 3000]},
                timeout=TIMEOUT,
            )
            if not response.ok:
                print("Telegram HTTP", response.status_code, flush=True)
        except requests.RequestException:
            print("Erreur reseau Telegram.", flush=True)


def api_get(path):
    """Payload JSON ou None. Compte chaque appel."""
    global CALLS, LAST_CALL

    for attempt in range(3):
        if CALLS >= MAX_CALLS or time.monotonic() - STARTED > MAX_RUNTIME_SECONDS:
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        LAST_CALL = time.monotonic()
        CALLS += 1

        try:
            response = SESSION.get(f"{BASE_URL}/{path}", timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue

        if response.status_code == 429:
            print("Limite de requetes atteinte : arret.", flush=True)
            return None

        if response.status_code in (401, 403):
            raise SystemExit(f"Cle API refusee (HTTP {response.status_code}).")

        if not response.ok:
            return None

        try:
            return response.json()
        except ValueError:
            return None

    return None


def fixtures_of(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def league_name(match):
    for key in ("league_name", "league", "competition", "tournament"):
        value = match.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict) and value.get("name"):
            return str(value["name"])
    return f"ligue {match.get('league_id')}"


def pick_sample(matches):
    """Un échantillon réparti sur le plus de ligues possible."""
    by_league = defaultdict(list)
    for match in matches:
        by_league[str(match.get("league_id"))].append(match)

    leagues = sorted(by_league, key=lambda k: -len(by_league[k]))
    sample = []

    for round_index in range(PER_LEAGUE):
        for league in leagues:
            if len(sample) >= SAMPLE_SIZE:
                return sample
            if round_index < len(by_league[league]):
                sample.append(by_league[league][round_index])

    return sample


# ---------- lecture de la réponse /stats (forme inconnue : on cherche) ----------

def find_team_blocks(node, found=None):
    """Tous les dictionnaires qui contiennent une clé 'shots' numérique."""
    if found is None:
        found = []
    if isinstance(node, dict):
        if number(node.get("shots")) is not None:
            found.append(node)
        for value in node.values():
            find_team_blocks(value, found)
    elif isinstance(node, list):
        for value in node:
            find_team_blocks(value, found)
    return found


def find_flag(node, key="available"):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], bool):
            return node[key]
        for value in node.values():
            result = find
