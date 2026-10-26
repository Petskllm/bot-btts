import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://api.football-data.org/v4"
PARIS = ZoneInfo("Europe/Paris")

MIN_SCORE = 60.0
MAX_ENVOIS = 10
MAX_API_CALLS = 60

# Plan gratuit football-data.org : 10 requêtes/minute.
# 6.5 s laisse une marge sous cette limite.
MIN_INTERVAL = 6.5
MAX_RETRIES = 2
REQUEST_TIMEOUT = 25

LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "PPL": "Primeira Liga",
    "DED": "Eredivisie",
    "BSA": "Serie A Brésil",
}

# Poids du modèle. Le score sert à classer les matchs et n'est pas
# une probabilité calibrée sans validation historique.
RECENT_MATCHES = 8
MIN_RECENT_MATCHES = 4
MIN_SPLIT_MATCHES = 4
STANDINGS_WEIGHT = 0.55
RECENT_WEIGHT = 0.45

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "btts-telegram-bot/3.0"})

CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = 0.0


# ================= TELEGRAM =================

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret TELEGRAM_TOKEN ou CHAT_ID absent.")

    # Telegram limite la longueur d'un message. Couper de préférence aux lignes.
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3500:
            if current:
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
            # Ne jamais inclure le token Telegram dans les logs.
            raise RuntimeError(
                f"Erreur Telegram : HTTP {response.status_code}"
            )


# ================= DIAGNOSTICS ET API =================

def add_error(message):
    message = str(message)
    for secret in (API_KEY, TOKEN):
        if secret:
            message = message.replace(secret, "[MASQUÉ]")
    message = message[:300]
    if message not in ERRORS:
        ERRORS.append(message)
    print(f"DIAGNOSTIC : {message}", flush=True)


def api_get(endpoint, params=None):
    """GET avec cache, limite globale d'appels et temporisation."""
    global API_CALLS, API_BLOCKED, LAST_CALL

    params = params or {}
    cache_key = (endpoint, tuple(sorted(params.items())))

    if cache_key in CACHE:
        return CACHE[cache_key]
    if API_BLOCKED:
        return None
    if not API_KEY:
        add_error("Secret API_KEY absent.")
        return None
    if API_CALLS >= MAX_API_CALLS:
        add_error("Budget maximal d'appels API atteint.")
        return None

    for attempt in range(MAX_RETRIES + 1):
        wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
        if wait > 0:
            time.sleep(wait)

        # Chaque tentative réseau compte dans le budget.
        if API_CALLS >= MAX_API_CALLS:
            add_error("Budget API atteint avant une nouvelle tentative.")
            return None

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
            return None

        if response.status_code == 429:
            # Respecte le quota : une pause puis une seule nouvelle tentative
            # si le budget le permet.
            if attempt < MAX_RETRIES and API_CALLS < MAX_API_CALLS:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    pause = max(60, int(retry_after))
                except (TypeError, ValueError):
                    pause = 60
                add_error(f"{endpoint} : quota HTTP 429, pause {pause} s.")
                time.sleep(pause)
                continue
            add_error(f"{endpoint} : HTTP 429, quota API dépassé.")
            API_BLOCKED = True
            return None

        if response.status_code in (401, 403):
            add_error(
                f"{endpoint} : HTTP {response.status_code}, "
                "clé invalide ou ressource non autorisée par le forfait."
            )
            API_BLOCKED = True
            return None

        if not response.ok:
            add_error(f"{endpoint} : HTTP {response.status_code}.")
            return None

        try:
            payload = response.json()
        except ValueError:
            add_error(f"{endpoint} : réponse JSON invalide.")
            return None

        CACHE[cache_key] = payload
        return payload

    return None


def diagnostics_text():
    if not ERRORS:
        return ""
    return "\n\nDiagnostic API :\n" + "\n".join(
        f"• {error}" for error in ERRORS[:4]
    )


# ================= MATCHS DU JOUR =================

def kickoff_of(match):
    raw = match["utcDate"]
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(PARIS)


def get_today_fixtures():
    today = datetime.now(PARIS).date()
    data = api_get("matches", {"date": today.isoformat()})
    matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(matches, list):
        return None, 0, 0

    selected = []
    league_count = 0
    seen = set()
    now = datetime.now(PARIS)

    for fixture in matches:
        try:
            code = fixture["competition"]["code"]
            if code not in LEAGUES:
                continue

            league_count += 1
            kickoff = kickoff_of(fixture)

            if kickoff.date() != today:
                continue
            if fixture.get("status") not in {"TIMED", "SCHEDULED"}:
                continue
            if kickoff <= now:
                continue
            if fixture.get("id") in seen:
                continue

            seen.add(fixture["id"])
            selected.append(fixture)
        except (KeyError, TypeError, ValueError):
            continue

    selected.sort(key=kickoff_of)
    return selected, len(matches), league_count


# ================= STATISTIQUES =================

def get_standings(code):
    data = api_get(f"competitions/{code}/standings")
    if not isinstance(data, dict):
        return None

    result = {}
    for block in data.get("standings") or []:
        kind = block.get("type")
        if not kind:
            continue
        for row in block.get("table") or []:
            try:
                team_id = row["team"]["id"]
                result.setdefault(kind, {})[team_id] = {
                    "played": int(row.get("playedGames") or 0),
                    "gf": int(row.get("goalsFor") or 0),
                    "ga": int(row.get("goalsAgainst") or 0),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return result or None


def extract_rates(standings, team_id, side):
    """Renvoie les buts marqués/encaissés par match et la taille d'échantillon."""
    total = standings.get("TOTAL", {}).get(team_id)
    if not total or total["played"] < 3:
        return None

    split = standings.get(side.upper(), {}).get(team_id)
    if split and split["played"] >= MIN_SPLIT_MATCHES:
        row, scope = split, side
    else:
        row, scope = total, "total"

    if row["played"] <= 0:
        return None

    return {
        "gf": row["gf"] / row["played"],
        "ga": row["ga"] / row["played"],
        "played": row["played"],
        "scope": scope,
    }


def get_recent_form(team_id):
    now = datetime.now(PARIS)
    date_to = now.date()
    date_from = date_to - timedelta(days=240)

    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "limit": 100,
    })
    raw_matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(raw_matches, list):
        return None

    matches = []
    for match in raw_matches:
        try:
            # Exclut les matchs sans score final complet.
            full_time = match["score"]["fullTime"]
            goals_home = full_time.get("home")
            goals_away = full_time.get("away")
            if goals_home is None or goals_away is None:
                continue

            date = kickoff_of(match)
            if date > now:
                continue

            home_id = match["homeTeam"]["id"]
            away_id = match["awayTeam"]["id"]
            if team_id == home_id:
                gf, ga, venue = goals_home, goals_away, "HOME"
            elif team_id == away_id:
                gf, ga, venue = goals_away, goals_home, "AWAY"
            else:
                continue

            matches.append({
                "gf": int(gf),
                "ga": int(ga),
                "date": date,
                "venue": venue,
                "btts": int(gf) > 0 and int(ga) > 0,
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    # Déduplique les rencontres puis conserve les plus récentes.
    unique = {m["date"].isoformat(): m for m in matches}
    matches = sorted(unique.values(), key=lambda m: m["date"], reverse=True)
    matches = matches[:RECENT_MATCHES]

    if len(matches) < MIN_RECENT_MATCHES:
        return None

    n = len(matches)
    return {
        "n": n,
        "gf": sum(m["gf"] for m in matches) / n,
        "ga": sum(m["ga"] for m in matches) / n,
        "btts": sum(m["btts"] for m in matches) / n,
        "btts_count": sum(m["btts"] for m in matches),
        "last_date": matches[0]["date"],
    }


# ================= MODÈLE BTTS =================

def poisson_score_probability(expected_goals):
    """P(au moins un but) sous une approximation de Poisson."""
    expected_goals = max(0.0, min(6.0, expected_goals))
    return 1.0 - math.exp(-expected_goals)


def calculate_score(fixture):
    home = fixture.get("homeTeam") or {}
    away = fixture.get("awayTeam") or {}
    competition = fixture.get("competition") or {}
    code = competition.get("code")

    if not home.get("id") or not away.get("id") or not code:
        return None

    standings = get_standings(code)
    if not standings:
        return None

    home_rates = extract_rates(standings, home["id"], "home")
    away_rates = extract_rates(standings, away["id"], "away")
    if not home_rates or not away_rates:
        return None

    home_form = get_recent_form(home["id"])
    away_form = get_recent_form(away["id"])
    if not home_form or not away_form:
        return None

    # Estimation des buts attendus : attaque de l'équipe et défense adverse.
    # Les statistiques de classement sont spécifiques au lieu si possible.
    home_attack = (
        STANDINGS_WEIGHT * home_rates["gf"]
        + RECENT_WEIGHT * home_form["gf"]
    )
    away_defence = (
        STANDINGS_WEIGHT * away_rates["ga"]
        + RECENT_WEIGHT * away_form["ga"]
    )
    away_attack = (
        STANDINGS_WEIGHT * away_rates["gf"]
        + RECENT_WEIGHT * away_form["gf"]
    )
    home_defence = (
        STANDINGS_WEIGHT * home_rates["ga"]
        + RECENT_WEIGHT * home_form["ga"]
    )

    # Moyenne des signaux, bornée pour éviter les valeurs aberrantes.
    expected_home = max(0.05, min(4.0, (home_attack + away_defence) / 2))
    expected_away = max(0.05, min(4.0, (away_attack + home_defence) / 2))

    p_home_scores = poisson_score_probability(expected_home)
    p_away_scores = poisson_score_probability(expected_away)
    model_rate = p_home_scores * p_away_scores

    # Ajustement modéré par le taux BTTS récent, sans ajouter un bonus brut.
    recent_rate = (home_form["btts"] + away_form["btts"]) / 2
    blended_rate = 0.80 * model_rate + 0.20 * recent_rate
    score = round(100 * max(0.0, min(1.0, blended_rate)), 1)

    warnings = []
    if home_rates["scope"] == "total" or away_rates["scope"] == "total":
        warnings.append("échantillon domicile/extérieur limité")
    if home_rates["played"] < 6 or away_rates["played"] < 6:
        warnings.append("peu de matchs au classement")
    if home_form["n"] < RECENT_MATCHES or away_form["n"] < RECENT_MATCHES:
        warnings.append("forme récente basée sur moins de 8 matchs")

    kickoff = kickoff_of(fixture)
    for form in (home_form, away_form):
        rest_hours = (kickoff - form["last_date"]).total_seconds() / 3600
        if 0 < rest_hours < 72:
            warnings.append("repos inférieur à 72 h")
            break

    home_name = home.get("shortName") or home.get("name") or "Équipe domicile"
    away_name = away.get("shortName") or away.get("name") or "Équipe extérieure"

    return {
        "id": fixture["id"],
        "match": f"{home_name} - {away_name}",
        "league": competition.get("name") or LEAGUES.get(code, code),
        "kickoff": kickoff,
        "score": score,
        "expected_home": expected_home,
        "expected_away": expected_away,
        "reason": (
            f"BTTS récents : {home_name} {home_form['btts_count']}/{home_form['n']} | "
            f"{away_name} {away_form['btts_count']}/{away_form['n']}"
        ),
        "warnings": list(dict.fromkeys(warnings)),
    }


# ================= MESSAGE ET EXÉCUTION =================

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Ajoute les secrets TELEGRAM_TOKEN et CHAT_ID.")
    if not API_KEY:
        send_message("BTTS : secret GitHub API_KEY absent.")
        raise RuntimeError("API_KEY absent.")

    now = datetime.now(PARIS)
    today = now.strftime("%d/%m/%Y")

    fixtures, total, league_total = get_today_fixtures()
    if fixtures is None:
        send_message(
            f"⚽ BTTS — {today}\n\nImpossible de récupérer les matchs."
            + diagnostics_text()
        )
        return

    if not fixtures:
        send_message(
            f"⚽ BTTS — {today}\n\n"
            f"Matchs renvoyés par l'API : {total}\n"
            f"Matchs des compétitions suivies : {league_total}\n"
            "Aucun match à venir aujourd'hui."
            + diagnostics_text()
        )
        return

    evaluated = []
    insufficient = 0

    for fixture in fixtures:
        if API_BLOCKED or API_CALLS >= MAX_API_CALLS:
            add_error("Analyse partielle : budget API ou accès API indisponible.")
            break
        try:
            result = calculate_score(fixture)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            add_error(f"Analyse d'un match impossible ({type(exc).__name__}).")
            result = None

        if result is None:
            insufficient += 1
            continue

        evaluated.append(result)
        print(f"{result['match']} : {result['score']}/100", flush=True)

    evaluated.sort(key=lambda item: item["score"], reverse=True)
    selected = [r for r in evaluated if r["score"] >= MIN_SCORE][:MAX_ENVOIS]
    selected_ids = {r["id"] for r in selected}
    others = [r for r in evaluated if r["id"] not in selected_ids][:6]

    summary = (
        f"À venir : {len(fixtures)} | analysés : {len(evaluated)} | "
        f"données insuffisantes : {insufficient} | appels API : {API_CALLS}/{MAX_API_CALLS}"
    )

    lines = [
        f"⚽ BTTS OUI — {today}",
        f"Filtre : indice ≥ {MIN_SCORE:.0f}/100",
        "",
    ]

    if selected:
        for index, result in enumerate(selected, 1):
            lines.extend([
                f"{index}. {result['match']}",
                f"🏆 {result['league']} | {result['kickoff']:%H:%M} Paris | "
                f"indice {result['score']}/100",
                f"⚽ Buts attendus estimés : {result['expected_home']:.2f} - "
                f"{result['expected_away']:.2f}",
                f"✅ {result['reason']}",
            ])
            if result["warnings"]:
                lines.append("⚠️ " + " ; ".join(result["warnings"]))
            lines.append("")
    else:
        lines.extend([f"Aucun indice ≥ {MIN_SCORE:.0f}/100.", ""])

    if others:
        lines.append("Autres matchs les mieux classés :")
        for result in others:
            lines.append(
                f"• {result['match']} — {result['score']}/100 "
                f"({result['kickoff']:%H:%M})"
            )
        lines.append("")

    lines.extend([
        summary,
        "",
        "Indice statistique non calibré : ce n'est pas une garantie ni une "
        "probabilité validée de gain.",
        "Vérifie les absences, les compositions et les cotes avant toute décision.",
    ])

    message = "\n".join(lines) + diagnostics_text()
    send_message(message)

    print(
        f"Terminé : {API_CALLS} appels API, {len(selected)} sélections.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Message générique : ne pas divulguer de secrets dans les logs Telegram.
        print(f"ERREUR FATALE : {type(exc).__name__}: {exc}", flush=True)
        raise

