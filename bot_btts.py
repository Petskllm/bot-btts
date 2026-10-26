import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")  # clé football-data.org

BASE_URL = "https://api.football-data.org/v4"
PARIS = ZoneInfo("Europe/Paris")

MIN_PERCENT = 62          # seuil minimum affiché en sélection
MAX_ENVOIS = 10           # nombre maximum de matchs sélectionnés
MAX_OTHERS = 4            # matchs "juste en dessous" affichés

# Plan gratuit football-data.org : 10 requêtes par minute.
MAX_API_CALLS = 60
MIN_INTERVAL = 6.5
MAX_RETRIES = 2
REQUEST_TIMEOUT = 25
MAX_CONSECUTIVE_FAILS = 4

LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Ligue des champions",
    "PPL": "Primeira Liga",
    "DED": "Eredivisie",
    "BSA": "Brasileirão",
}

# ----- Modèle (statistiques de matchs uniquement, aucune cote) -----
HISTORY_DAYS = 240        # fenêtre de recherche des matchs passés
OVERALL_N = 10            # derniers matchs pris en compte
VENUE_N = 6               # derniers matchs au même lieu (dom. / ext.)
MIN_OVERALL = 6           # minimum de matchs pour analyser une équipe
MIN_VENUE = 3             # en dessous : avertissement "peu de données"
GOOD_SAMPLE = 8
DECAY = 0.90              # les matchs récents comptent plus
VENUE_SHRINK = 3.0        # poids de l'ensemble des matchs vs lieu précis
PRIOR_SHRINK = 3.0        # poids de la moyenne de la ligue
DEFAULT_GOALS = 1.35      # buts par équipe et par match si inconnu
MAX_STALE_DAYS = 60       # dernier match trop ancien = données peu fiables
SHORT_REST_HOURS = 72

W_POISSON = 0.40
W_RATES = 0.35
W_HISTORY = 0.25

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "btts-telegram-bot/4.0"})

CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = None
CONSECUTIVE_FAILS = 0

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
           "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# ================= TELEGRAM =================

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret TELEGRAM_TOKEN ou CHAT_ID absent.")

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > 3500:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:3500])
            line = line[3500:]

        if len(current) + len(line) > 3500:
            chunks.append(current)
            current = ""

        current += line

    if current:
        chunks.append(current)

    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk},
            timeout=REQUEST_TIMEOUT,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or not payload.get("ok"):
            # Ne jamais afficher le token Telegram dans les logs.
            raise RuntimeError(
                f"Erreur Telegram : HTTP {response.status_code}"
            )


# ================= API =================

def add_error(message):
    message = str(message)

    for secret in (API_KEY, TOKEN):
        if secret:
            message = message.replace(secret, "[MASQUÉ]")

    message = message[:200]

    if message not in ERRORS:
        ERRORS.append(message)

    print(f"DIAGNOSTIC : {message}", flush=True)


def note_failure():
    """Après plusieurs échecs d'affilée, on arrête de gaspiller des appels."""
    global CONSECUTIVE_FAILS, API_BLOCKED

    CONSECUTIVE_FAILS += 1

    if CONSECUTIVE_FAILS >= MAX_CONSECUTIVE_FAILS:
        API_BLOCKED = True
        add_error("Trop d'échecs d'affilée, analyse arrêtée.")


def api_get(endpoint, params=None):
    """GET avec cache, budget d'appels et respect de la limite."""
    global API_CALLS, API_BLOCKED, LAST_CALL, CONSECUTIVE_FAILS

    params = params or {}
    cache_key = (endpoint, tuple(sorted(params.items())))

    if cache_key in CACHE:
        return CACHE[cache_key]

    if API_BLOCKED:
        return None

    if not API_KEY:
        add_error("Secret API_KEY absent.")
        return None

    for attempt in range(MAX_RETRIES + 1):
        if API_CALLS >= MAX_API_CALLS:
            add_error("Budget maximal d'appels API atteint.")
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)

            if wait > 0:
                time.sleep(wait)

        API_CALLS += 1
        LAST_CALL = time.monotonic()
        print(f"API {API_CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            response = SESSION.get(
                f"{BASE_URL}/{endpoint}",
                headers={"X-Auth-Token": API_KEY},
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            add_error(f"{endpoint} : erreur réseau ({type(exc).__name__}).")

            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
                continue

            note_failure()
            return None

        if response.status_code == 429:
            if attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After", "")

                try:
                    pause = max(60, int(retry_after))
                except (TypeError, ValueError):
                    pause = 60

                add_error(f"{endpoint} : quota atteint, pause {pause} s.")
                time.sleep(pause)
                continue

            add_error(f"{endpoint} : quota API dépassé.")
            API_BLOCKED = True
            return None

        if response.status_code == 401:
            add_error("Clé API refusée (401).")
            API_BLOCKED = True
            return None

        if not response.ok:
            # 403 / 404 / 400 : on ignore cette ressource, sans tout bloquer.
            add_error(f"{endpoint} : HTTP {response.status_code}.")
            note_failure()
            return None

        try:
            payload = response.json()
        except ValueError:
            add_error(f"{endpoint} : réponse JSON invalide.")
            note_failure()
            return None

        if not isinstance(payload, dict):
            add_error(f"{endpoint} : format de réponse invalide.")
            note_failure()
            return None

        CONSECUTIVE_FAILS = 0
        CACHE[cache_key] = payload
        return payload

    return None


def diagnostics_text():
    if not ERRORS:
        return ""

    return "\n\n🛠️ Problème technique : " + " ; ".join(ERRORS[:2])


# ================= MATCHS DU JOUR =================

def kickoff_of(match):
    raw = match["utcDate"]
    return datetime.fromisoformat(
        raw.replace("Z", "+00:00")
    ).astimezone(PARIS)


def get_today_fixtures():
    now = datetime.now(PARIS)
    today = now.date()

    data = api_get("matches", {
        "dateFrom": today.isoformat(),
        "dateTo": (today + timedelta(days=1)).isoformat(),
    })

    matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(matches, list):
        return None, 0, 0

    selected = []
    league_count = 0
    seen = set()

    for fixture in matches:
        try:
            if fixture["competition"]["code"] not in LEAGUES:
                continue

            kickoff = kickoff_of(fixture)

            if kickoff.date() != today:
                continue

            league_count += 1

            if fixture.get("status") not in {"TIMED", "SCHEDULED"}:
                continue

            if kickoff <= now:
                continue

            fixture_id = fixture["id"]

            if fixture_id in seen:
                continue

            seen.add(fixture_id)
            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            continue

    selected.sort(key=kickoff_of)

    return selected, len(matches), league_count


# ================= DONNÉES STATISTIQUES =================

def get_league_prior(code):
    """Buts moyens par équipe et par match dans la compétition."""
    key = ("prior", code)

    if key in CACHE:
        return CACHE[key]

    prior = DEFAULT_GOALS
    data = api_get(f"competitions/{code}/standings")

    if isinstance(data, dict):
        goals = 0
        played = 0

        for block in data.get("standings") or []:
            if block.get("type") != "TOTAL":
                continue

            for row in block.get("table") or []:
                try:
                    goals += int(row.get("goalsFor") or 0)
                    played += int(row.get("playedGames") or 0)
                except (TypeError, ValueError):
                    continue

        if played >= 20:
            prior = goals / played

    prior = max(0.9, min(2.2, prior))
    CACHE[key] = prior

    return prior


def regular_goals(match):
    """Score après 90 minutes (le marché BTTS ignore la prolongation)."""
    score = match.get("score") or {}

    regular = score.get("regularTime") or {}

    if regular.get("home") is not None and regular.get("away") is not None:
        return int(regular["home"]), int(regular["away"])

    if score.get("duration") in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
        return None

    full = score.get("fullTime") or {}

    if full.get("home") is not None and full.get("away") is not None:
        return int(full["home"]), int(full["away"])

    return None


def get_team_history(team_id):
    """Matchs terminés d'une équipe (toutes compétitions), récents d'abord."""
    now = datetime.now(PARIS)

    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
        "dateFrom": (now.date() - timedelta(days=HISTORY_DAYS)).isoformat(),
        "dateTo": now.date().isoformat(),
        "limit": 100,
    })

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    unique = {}

    for match in raw_matches:
        try:
            if match.get("status") != "FINISHED":
                continue

            goals = regular_goals(match)

            if goals is None:
                continue

            date = kickoff_of(match)

            if date > now:
                continue

            home_id = match["homeTeam"]["id"]
            away_id = match["awayTeam"]["id"]

            if team_id == home_id:
                venue, gf, ga = "home", goals[0], goals[1]
            elif team_id == away_id:
                venue, gf, ga = "away", goals[1], goals[0]
            else:
                continue

            unique[match["id"]] = {
                "gf": gf,
                "ga": ga,
                "date": date,
                "venue": venue,
            }

        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    return sorted(
        unique.values(),
        key=lambda item: item["date"],
        reverse=True,
    )


# ================= MODÈLE BTTS =================

METRICS = ("gf", "ga", "scored", "conceded", "btts")


def shrink(value, n, prior, k):
    """Ramène une valeur vers la référence quand l'échantillon est petit."""
    return (value * n + prior * k) / (n + k)


def weighted_stats(matches):
    weights = [DECAY ** index for index in range(len(matches))]
    total = sum(weights)

    def average(function):
        return sum(
            weight * function(match)
            for weight, match in zip(weights, matches)
        ) / total

    return {
        "n": len(matches),
        "gf": average(lambda m: m["gf"]),
        "ga": average(lambda m: m["ga"]),
        "scored": average(lambda m: 1.0 if m["gf"] > 0 else 0.0),
        "conceded": average(lambda m: 1.0 if m["ga"] > 0 else 0.0),
        "btts": average(
            lambda m: 1.0 if m["gf"] > 0 and m["ga"] > 0 else 0.0
        ),
    }


def build_profile(history, venue, prior):
    recent = history[:OVERALL_N]

    if len(recent) < MIN_OVERALL:
        return None

    overall = weighted_stats(recent)
    same_venue = [m for m in history if m["venue"] == venue][:VENUE_N]

    if same_venue:
        local = weighted_stats(same_venue)
        blended = {
            key: shrink(local[key], local["n"], overall[key], VENUE_SHRINK)
            for key in METRICS
        }
    else:
        blended = {key: overall[key] for key in METRICS}

    prior_scored = 1.0 - math.exp(-prior)
    references = {
        "gf": prior,
        "ga": prior,
        "scored": prior_scored,
        "conceded": prior_scored,
        "btts": prior_scored ** 2,
    }

    profile = {
        key: shrink(blended[key], overall["n"], references[key], PRIOR_SHRINK)
        for key in METRICS
    }

    profile.update({
        "n": overall["n"],
        "n_venue": len(same_venue),
        "btts_count": sum(
            1 for m in recent if m["gf"] > 0 and m["ga"] > 0
        ),
        "last_date": history[0]["date"],
    })

    return profile


def predict_btts(home, away):
    # 1) Buts attendus (loi de Poisson).
    lambda_home = max(0.2, min(3.5, (home["gf"] + away["ga"]) / 2))
    lambda_away = max(0.2, min(3.5, (away["gf"] + home["ga"]) / 2))

    p_poisson = (
        (1.0 - math.exp(-lambda_home))
        * (1.0 - math.exp(-lambda_away))
    )

    # 2) Fréquence réelle : marquer / encaisser.
    p_home_scores = (home["scored"] + away["conceded"]) / 2
    p_away_scores = (away["scored"] + home["conceded"]) / 2
    p_rates = p_home_scores * p_away_scores

    # 3) Historique BTTS des deux équipes.
    p_history = (home["btts"] + away["btts"]) / 2

    probability = (
        W_POISSON * p_poisson
        + W_RATES * p_rates
        + W_HISTORY * p_history
    )

    probability = max(0.05, min(0.95, probability))

    return probability, {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "poisson": p_poisson,
        "rates": p_rates,
        "history": p_history,
    }


def analyse_fixture(fixture):
    home_team = fixture.get("homeTeam") or {}
    away_team = fixture.get("awayTeam") or {}
    competition = fixture.get("competition") or {}
    code = competition.get("code")

    if not home_team.get("id") or not away_team.get("id") or not code:
        return None

    prior = get_league_prior(code)

    home_history = get_team_history(home_team["id"])
    away_history = get_team_history(away_team["id"])

    if not home_history or not away_history:
        return None

    home = build_profile(home_history, "home", prior)
    away = build_profile(away_history, "away", prior)

    if not home or not away:
        return None

    kickoff = kickoff_of(fixture)

    # Données trop anciennes (trêve longue, début de saison) : on s'abstient.
    for profile in (home, away):
        if (kickoff - profile["last_date"]).days > MAX_STALE_DAYS:
            return None

    probability, details = predict_btts(home, away)
    percent = int(round(probability * 100))

    home_name = home_team.get("shortName") or home_team.get("name") or "?"
    away_name = away_team.get("shortName") or away_team.get("name") or "?"

    warnings = []

    for name, profile in ((home_name, home), (away_name, away)):
        rest_hours = (
            kickoff - profile["last_date"]
        ).total_seconds() / 3600

        if 0 < rest_hours < SHORT_REST_HOURS:
            warnings.append(f"repos court ({name})")

    if (
        min(home["n"], away["n"]) < GOOD_SAMPLE
        or min(home["n_venue"], away["n_venue"]) < MIN_VENUE
    ):
        warnings.append("peu de données")

    print(
        f"{home_name} - {away_name} : {percent}% "
        f"(buts attendus {details['lambda_home']:.2f}-"
        f"{details['lambda_away']:.2f} | "
        f"poisson {details['poisson']:.2f} | "
        f"fréquence {details['rates']:.2f} | "
        f"historique {details['history']:.2f})",
        flush=True,
    )

    return {
        "id": fixture["id"],
        "match": f"{home_name} - {away_name}",
        "league": LEAGUES.get(code, competition.get("name") or code),
        "kickoff": kickoff,
        "percent": percent,
        "form": (
            f"{home['btts_count']}/{home['n']} et "
            f"{away['btts_count']}/{away['n']}"
        ),
        "warnings": warnings,
    }


# ================= MESSAGE TELEGRAM =================

def build_message(today, selected, others, analysed, total, partial):
    lines = [
        f"⚽ BTTS OUI — {today}",
        f"🎯 Seuil : {MIN_PERCENT} % minimum",
        "",
    ]

    if selected:
        for index, result in enumerate(selected):
            number = NUMBERS[index] if index < len(NUMBERS) else "▫️"
            icon = "🔥" if result["percent"] >= 70 else "✅"

            lines.append(f"{number} {result['match']}")
            lines.append(
                f"🏆 {result['league']} · 🕘 {result['kickoff']:%H:%M}"
            )
            lines.append(f"{icon} BTTS : {result['percent']} %")
            lines.append(f"📊 Derniers matchs : {result['form']}")

            if result["warnings"]:
                lines.append("⚠️ " + " ; ".join(result["warnings"]))

            lines.append("")
    else:
        lines.append(f"😕 Aucun match à {MIN_PERCENT} % ou plus aujourd'hui.")
        lines.append("")

    if others:
        lines.append("👀 Juste en dessous :")

        for result in others:
            lines.append(
                f"• {result['match']} : {result['percent']} %"
            )

        lines.append("")

    lines.append(f"📈 {analysed}/{total} matchs analysés")

    if partial:
        lines.append("⏳ Analyse partielle (limite de requêtes).")

    lines.append("ℹ️ Estimation statistique, aucune garantie.")

    return "\n".join(lines) + diagnostics_text()


# ================= EXÉCUTION =================

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Ajoute les secrets TELEGRAM_TOKEN et CHAT_ID.")

    if not API_KEY:
        send_message("BTTS : secret GitHub API_KEY absent.")
        raise RuntimeError("API_KEY absent.")

    today = datetime.now(PARIS).strftime("%d/%m/%Y")

    fixtures, _, league_total = get_today_fixtures()

    if fixtures is None:
        send_message(
            f"⚽ BTTS — {today}\n\n"
            "❌ Impossible de récupérer les matchs du jour."
            + diagnostics_text()
        )
        return

    if not fixtures:
        send_message(
            f"⚽ BTTS — {today}\n\n"
            "😴 Aucun match à venir aujourd'hui dans les ligues suivies."
            + diagnostics_text()
        )
        return

    evaluated = []
    partial = False

    for fixture in fixtures:
        # Un match demande jusqu'à 3 appels (ligue + 2 équipes).
        if API_BLOCKED or API_CALLS + 3 > MAX_API_CALLS:
            partial = True
            break

        try:
            result = analyse_fixture(fixture)
        except (KeyError, TypeError, ValueError,
                ZeroDivisionError, OverflowError) as exc:
            add_error(f"Analyse d'un match impossible ({type(exc).__name__}).")
            result = None

        if result is not None:
            evaluated.append(result)

    evaluated.sort(key=lambda item: item["percent"], reverse=True)

    selected = [
        item for item in evaluated if item["percent"] >= MIN_PERCENT
    ][:MAX_ENVOIS]

    selected_ids = {item["id"] for item in selected}

    others = [
        item for item in evaluated if item["id"] not in selected_ids
    ][:MAX_OTHERS]

    send_message(
        build_message(
            today,
            selected,
            others,
            len(evaluated),
            len(fixtures),
            partial,
        )
    )

    print(
        f"Terminé : {API_CALLS} appels API, "
        f"{len(selected)} sélections, "
        f"{league_total} matchs suivis aujourd'hui.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Message volontairement générique : pas de secrets dans les logs.
        print(f"ERREUR FATALE : {type(exc).__name__}", flush=True)
        raise
