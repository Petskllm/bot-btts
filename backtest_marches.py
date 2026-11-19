"""
BACKTEST DES MARCHES A COTES PLUS HAUTES — SportAPI

Rejoue le modèle "comme à l'époque" sur des matchs déjà joués (seules les
données antérieures à chaque match sont utilisées) et mesure, pour chaque
marché et chaque tranche de probabilité, le taux de réussite réel.

Marchés : plus de 2,5 buts, plus de 3,5 buts, équipe domicile +1,5 but,
équipe extérieur +1,5 but, et plus de 1,5 but (référence).

Pour chaque tranche on donne la "cote seuil" (1 / taux réel) : en dessous,
le pari perd de l'argent sur la durée. Les cotes du bookmaker ne sont PAS
connues ici : c'est à toi de les comparer.

Coût : 1 requête par jour récupéré (DAYS_BACK, 150 par défaut).
Lancement : manuel (workflow "Backtest des marchés"), jamais automatique.
Ne modifie ni le bot ni le journal.
"""

import bisect
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

DAYS_BACK = int(os.getenv("DAYS_BACK", "150"))   # jours récupérés
TEST_DAYS = int(os.getenv("TEST_DAYS", "70"))    # derniers jours testés

MIN_PROB = 0.72
MAX_PROB = 0.85
MAX_MARKETS = 2

MAX_CALLS = DAYS_BACK + 40
MIN_INTERVAL = 0.3
TIMEOUT = 25
MAX_RUNTIME_SECONDS = 1500

MAX_HISTORY_DAYS = 240
MAX_STALE_DAYS = 60
LOOKBACK_ITEMS = 40

EXCLUDE_YOUTH = True
YOUTH_PATTERN = re.compile(
    r"\bU[- ]?(1[5-9]|2[0-3])\b|\byouth\b|\bjuniors?\b"
    r"|\bprimavera\b|\breserves?\b|\bamateurs?\b|\bII$",
    re.IGNORECASE,
)

PRIORS = {"gf": 1.35, "ga": 1.35, "btts": 0.52, "over15": 0.75}

# Paramètres actuels du bot (point de départ)
CURRENT = {
    "history_n": 10,
    "min_history": 6,
    "venue_n": 6,
    "decay": 0.90,
    "prior_k": 4.0,
    "venue_cap": 0.50,
}

# Valeurs essayées une par une pour chaque paramètre
GRID = {
    "history_n": [6, 8, 10, 12, 15],
    "decay": [0.80, 0.85, 0.90, 0.95, 1.00],
    "prior_k": [0, 2, 4, 8, 12],
    "venue_cap": [0.0, 0.25, 0.50],
}

MARKETS = ("over25", "over35", "home15", "away15", "over15")
LABELS = {
    "over25": "Plus de 2,5 buts",
    "over35": "Plus de 3,5 buts",
    "home15": "Domicile +1,5 but",
    "away15": "Exterieur +1,5 but",
    "over15": "Plus de 1,5 but (reference)",
}

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-backtest/1.0",
})

STARTED = time.monotonic()
CALLS = 0
LAST_CALL = None


# ================= UTILITAIRES =================

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


def paris_date(match):
    raw = match.get("kickoff_utc")

    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))

            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            return value.astimezone(PARIS).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(str(match.get("date"))[:10]).date()
    except ValueError:
        return None


def is_youth(match):
    return any(
        YOUTH_PATTERN.search(str(match.get(field) or ""))
        for field in ("home_team", "away_team")
    )


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


# ================= RECUPERATION DES MATCHS =================

def fetch_day(day):
    """Matchs d'une date (1 requête). None si échec."""
    global CALLS, LAST_CALL

    for attempt in range(3):
        if CALLS >= MAX_CALLS:
            print("Budget de requetes du backtest atteint.", flush=True)
            return None

        if time.monotonic() - STARTED > MAX_RUNTIME_SECONDS:
            print("Duree maximale atteinte.", flush=True)
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        LAST_CALL = time.monotonic()
        CALLS += 1

        try:
            response = SESSION.get(
                f"{BASE_URL}/fixtures/date/{day.isoformat()}",
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue

        if response.status_code == 429:
            try:
                pause = max(60, int(response.headers.get("Retry-After", "")))
            except (TypeError, ValueError):
                pause = 60

            if time.monotonic() - STARTED + pause > MAX_RUNTIME_SECONDS:
                print("Limite de requetes : arret.", flush=True)
                return None

            print(f"Limite atteinte, pause {pause} s.", flush=True)
            time.sleep(pause)
            continue

        if response.status_code in (401, 403):
            raise SystemExit(f"Cle API refusee (HTTP {response.status_code}).")

        if not response.ok:
            time.sleep(2 * (attempt + 1))
            continue

        try:
            payload = response.json()
        except ValueError:
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else None

    return None


def collect_matches(today):
    matches = {}
    days_ok = 0

    for offset in range(DAYS_BACK, 0, -1):
        day = today - timedelta(days=offset)
        data = fetch_day(day)

        if data is None:
            continue

        days_ok += 1

        for item in data:
            if not isinstance(item, dict):
                continue

            if str(item.get("status") or "").strip().upper() != "FT":
                continue

            home_score = number(item.get("home_score"))
            away_score = number(item.get("away_score"))
            date = paris_date(item)

            if (
                home_score is None
                or away_score is None
                or home_score < 0
                or away_score < 0
                or date is None
                or item.get("home_id") is None
                or item.get("away_id") is None
            ):
                continue

            if EXCLUDE_YOUTH and is_youth(item):
                continue

            identifier = item.get("id")

            if identifier is None:
                identifier = (item["home_id"], item["away_id"], date)

            matches[identifier] = {
                "id": identifier,
                "date": date,
                "home_id": str(item["home_id"]),
                "away_id": str(item["away_id"]),
                "league_id": str(item.get("league_id")),
                "gh": int(home_score),
                "ga": int(away_score),
            }

        if days_ok % 20 == 0:
            print(
                f"{days_ok} jours recuperes, {len(matches)} matchs, "
                f"{CALLS} requetes.",
                flush=True,
            )

    print(
        f"Recuperation terminee : {days_ok} jours, {len(matches)} matchs "
        f"termines, {CALLS} requetes.",
        flush=True,
    )

    return sorted(matches.values(), key=lambda m: (m["date"], str(m["id"])))


# ================= HISTORIQUE "A L'EPOQUE" =================

class History:
    def __init__(self, matches):
        self.items = defaultdict(list)

        for match in matches:
            self.items[match["home_id"]].append(
                (match["date"], str(match["id"]), "home", match["gh"], match["ga"])
            )
            self.items[match["away_id"]].append(
                (match["date"], str(match["id"]), "away", match["ga"], match["gh"])
            )

        self.dates = {}

        for team, rows in self.items.items():
            rows.sort()
            self.dates[team] = [row[0] for row in rows]

    def before(self, team, day):
        rows = self.items.get(team)

        if not rows:
            return []

        index = bisect.bisect_left(self.dates[team], day)
        chunk = rows[max(0, index - LOOKBACK_ITEMS):index]

        return [
            {"date": d, "venue": v, "gf": gf, "ga": ga}
            for d, _, v, gf, ga in reversed(chunk)
            if (day - d).days <= MAX_HISTORY_DAYS
        ]


# ================= MODELE (copie du bot) =================

def weighted_stats(matches, decay):
    weights = [decay ** i for i in range(len(matches))]
    total = sum(weights)

    def average(function):
        return sum(
            w * function(m) for w, m in zip(weights, matches)
        ) / total

    return {
        "gf": average(lambda m: m["gf"]),
        "ga": average(lambda m: m["ga"]),
        "btts": average(lambda m: float(m["gf"] > 0 and m["ga"] > 0)),
        "over15": average(lambda m: float(m["gf"] + m["ga"] >= 2)),
    }


def profile(history, venue, day, P):
    recent = history[:P["history_n"]]

    if len(recent) < P["min_history"]:
        return None

    if (day - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    overall = weighted_stats(recent, P["decay"])
    local_matches = [m for m in history if m["venue"] == venue][:P["venue_n"]]

    if len(local_matches) >= 3:
        venue_weight = min(
            P["venue_cap"],
            len(local_matches) / (len(local_matches) + 6),
        )
        local = weighted_stats(local_matches, P["decay"])
    else:
        venue_weight = 0.0
        local = overall

    result = {
        key: (1 - venue_weight) * overall[key] + venue_weight * local[key]
        for key in overall
    }

    n = len(recent)

    for key, prior in PRIORS.items():
        result[key] = (result[key] * n + prior * P["prior_k"]) / (
            n + P["prior_k"]
        )

    return result



def estimate(home, away):
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2
    total = lambda_home + lambda_away

    return {
        "over25": 1 - math.exp(-total) * (1 + total + total ** 2 / 2),
        "over35": 1 - math.exp(-total) * (
            1 + total + total ** 2 / 2 + total ** 3 / 6
        ),
        "home15": 1 - math.exp(-lambda_home) * (1 + lambda_home),
        "away15": 1 - math.exp(-lambda_away) * (1 + lambda_away),
        "over15": (
            0.75 * (1 - math.exp(-total) * (1 + total))
            + 0.25 * (home["over15"] + away["over15"]) / 2
        ),
    }


# ================= EVALUATION =================

def prepare(test_matches, history):
    """Historiques "à l'époque" de chaque match testé (calculés une fois)."""
    prepared = []

    for match in test_matches:
        prepared.append({
            "match": match,
            "home_hist": history.before(match["home_id"], match["date"]),
            "away_hist": history.before(match["away_id"], match["date"]),
        })

    return prepared


def evaluate(prepared, P):
    """Une ligne par (match, marché) : probabilité prévue et résultat."""
    rows = []

    for item in prepared:
        match = item["match"]
        day = match["date"]

        home = profile(item["home_hist"], "home", day, P)
        away = profile(item["away_hist"], "away", day, P)

        if home is None or away is None:
            continue

        probabilities = estimate(home, away)
        goals = match["gh"] + match["ga"]
        outcomes = {
            "over25": goals >= 3,
            "over35": goals >= 4,
            "home15": match["gh"] >= 2,
            "away15": match["ga"] >= 2,
            "over15": goals >= 2,
        }

        for market in MARKETS:
            rows.append({
                "match": match,
                "market": market,
                "p": probabilities[market],
                "hit": 1.0 if outcomes[market] else 0.0,
            })

    return rows


def brier(rows):
    if not rows:
        return float("nan")
    return sum((r["p"] - r["hit"]) ** 2 for r in rows) / len(rows)


def baseline_brier(rows):
    """Brier d'une prévision constante (taux moyen du marché)."""
    if not rows:
        return float("nan")
    rate = sum(r["hit"] for r in rows) / len(rows)
    return sum((rate - r["hit"]) ** 2 for r in rows) / len(rows)


def fmt(value):
    return f"{value:.2f}".replace(".", ",")


BANDS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 1.01]
MIN_BAND_N = 25


def band_lines(rows, min_low=0.0):
    lines = []

    for low, high in zip(BANDS, BANDS[1:]):
        if low < min_low:
            continue
        subset = [r for r in rows if low <= r["p"] < high]
        if len(subset) < MIN_BAND_N:
            continue

        predicted = sum(r["p"] for r in subset) / len(subset)
        actual = sum(r["hit"] for r in subset) / len(subset)
        threshold = 1 / actual if actual > 0 else float("inf")
        lines.append(
            f"{low:.0%}-{min(high, 1):.0%} : n={len(subset)} "
            f"annonce {predicted:.0%} reel {actual:.0%} "
            f"({actual - predicted:+.0%}) | cote seuil {fmt(threshold)}"
        )

    return lines


# ================= PROGRAMME =================

def main():
    if not API_KEY:
        raise SystemExit("Secret API_KEY absent.")

    today = datetime.now(PARIS).date()
    matches = collect_matches(today)

    if len(matches) < 500:
        message = (
            "BACKTEST MARCHES : trop peu de matchs recuperes "
            f"({len(matches)}). Verifie la cle API et le quota."
        )
        print(message, flush=True)
        send_telegram(message)
        return

    history = History(matches)
    limit = today - timedelta(days=TEST_DAYS)
    test_matches = [m for m in matches if m["date"] >= limit]
    prepared = prepare(test_matches, history)
    rows_all = evaluate(prepared, CURRENT)

    middle = limit + timedelta(days=TEST_DAYS // 2)
    analysable = len({r["match"]["id"] for r in rows_all})

    report = []

    def out(line=""):
        print(line, flush=True)
        report.append(line)

    out("===== BACKTEST DES MARCHES =====")
    out(f"Matchs termines recuperes : {len(matches)}")
    out(f"Matchs analysables sur {TEST_DAYS} jours : {analysable}")
    out(f"Ligues concernees : {len({m['league_id'] for m in test_matches})}")

    summary = [
        "📊 BACKTEST DES MARCHES",
        f"{analysable} matchs sur {TEST_DAYS} jours",
        "Cote seuil = 1 / taux reel : le pari ne gagne de l'argent que si ton bookmaker paie plus.",
    ]

    for market in MARKETS:
        rows = [r for r in rows_all if r["market"] == market]
        if not rows:
            continue

        n = len(rows)
        predicted = sum(r["p"] for r in rows) / n
        actual = sum(r["hit"] for r in rows) / n
        base = baseline_brier(rows)
        skill = 1 - brier(rows) / base if base else float("nan")

        old = [r for r in rows if r["match"]["date"] < middle]
        new = [r for r in rows if r["match"]["date"] >= middle]
        gap_old = (
            sum(r["hit"] - r["p"] for r in old) / len(old) if old else float("nan")
        )
        gap_new = (
            sum(r["hit"] - r["p"] for r in new) / len(new) if new else float("nan")
        )

        head_line = (
            f"{LABELS[market]} : annonce {predicted:.0%} | reel {actual:.0%} "
            f"| gain vs constante {skill:+.1%}"
        )
        stab_line = (
            f"  stabilite : ecart ancienne moitie {gap_old:+.1%}, "
            f"recente moitie {gap_new:+.1%}"
        )

        out("\n--- " + LABELS[market] + " ---")
        out(head_line)
        out(stab_line)
        for line in band_lines(rows):
            out("  " + line)

        summary.append("")
        summary.append("• " + head_line)
        summary.append(stab_line)
        for line in band_lines(rows, min_low=0.30):
            summary.append("  " + line)

    send_telegram("\n".join(summary))
    print(f"\nTermine : {CALLS} requetes.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print("ERREUR FATALE :", type(exc).__name__, safe(exc), flush=True)
        raise SystemExit(1)
