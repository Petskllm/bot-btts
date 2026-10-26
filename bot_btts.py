# bot_btts.py
# Bot BTTS Oui — API-Football + Telegram + GitHub Actions

import os
import time
import sqlite3
import requests
import pytz
from datetime import datetime

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

API_HOST = "v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
    3: "Europa League",
    94: "Primeira Liga",
    88: "Eredivisie",
    203: "Süper Lig",
    71: "Serie A Brésil",
    128: "Ligue 1 Argentine",
}

MIN_SCORE = 65
MAX_ENVOIS = 10
MAX_ANALYSES = 20

PARIS_TZ = pytz.timezone("Europe/Paris")


# ====================== BASE DE DONNÉES ======================
def init_db():
    conn = sqlite3.connect("predictions.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            match TEXT,
            league TEXT,
            score REAL,
            result TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_prediction(date, match, league, score):
    conn = sqlite3.connect("predictions.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO predictions (date, match, league, score) VALUES (?, ?, ?, ?)",
        (date, match, league, score)
    )
    conn.commit()
    conn.close()


# ====================== API ======================
def api_get(endpoint, params=None):
    url = f"https://{API_HOST}/{endpoint}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        print(f"API {endpoint} → {r.status_code}")

        if r.status_code != 200:
            print(f"Erreur API: {r.text[:250]}")
            return []

        data = r.json()
        response = data.get("response", [])
        print(f"→ {len(response)} résultats")
        return response

    except Exception as e:
        print(f"Exception API {endpoint}: {e}")
        return []


def get_current_season():
    now = datetime.now(PARIS_TZ)
    return now.year if now.month >= 7 else now.year - 1


def get_fixtures_today():
    season = get_current_season()
    today = datetime.now(PARIS_TZ).strftime("%Y-%m-%d")

    print(f"Recherche des matchs du {today} — saison {season}")
    all_fixtures = []

    for league_id, league_name in LEAGUES.items():
        data = api_get("fixtures", {
            "league": league_id,
            "season": season,
            "date": today
        })

        print(f"{league_name}: {len(data)} matchs")
        all_fixtures.extend(data)
        time.sleep(0.4)

    print(f"TOTAL MATCHS DU JOUR: {len(all_fixtures)}")
    return all_fixtures[:MAX_ANALYSES]


def get_team_stats(team_id, league_id, season):
    data = api_get("teams/statistics", {
        "team": team_id,
        "league": league_id,
        "season": season
    })
    return data[0] if data else None


def get_h2h(team1_id, team2_id):
    return api_get("fixtures/headtohead", {
        "h2h": f"{team1_id}-{team2_id}",
        "last": 6
    })


def get_team_form(team_id, season):
    data = api_get("fixtures", {
        "team": team_id,
        "season": season,
        "last": 5
    })

    form = {
        "matchs": 0,
        "buts_marques": 0,
        "buts_encaisses": 0,
        "btts": 0
    }

    for match in data:
        goals = match.get("goals", {})
        home_goals = goals.get("home")
        away_goals = goals.get("away")

        if home_goals is None or away_goals is None:
            continue

        team_id_str = str(team_id)
        home_id_str = str(match["teams"]["home"]["id"])
        away_id_str = str(match["teams"]["away"]["id"])

        if team_id_str == home_id_str:
            form["buts_marques"] += home_goals
            form["buts_encaisses"] += away_goals
        else:
            form["buts_marques"] += away_goals
            form["buts_encaisses"] += home_goals

        if home_goals > 0 and away_goals > 0:
            form["btts"] += 1

        form["matchs"] += 1

    return form


def get_injuries(fixture_id):
    return api_get("injuries", {
        "fixture": fixture_id
    })


# ====================== CALCUL BTTS ======================
def calculate_btts_score(fixture):
    try:
        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]
        league_id = fixture["league"]["id"]
        season = fixture["league"]["season"]
        fixture_id = fixture["fixture"]["id"]

        home_stats = get_team_stats(home["id"], league_id, season)
        time.sleep(0.3)

        away_stats = get_team_stats(away["id"], league_id, season)
        time.sleep(0.3)

        if not home_stats or not away_stats:
            return 0, "Données insuffisantes", ""

        def extract(stats):
            played = stats["fixtures"]["played"]["total"] or 1
            gf = stats["goals"]["for"]["total"]["total"] or 0
            ga = stats["goals"]["against"]["total"]["total"] or 0
            return gf / played, ga / played, played

        home_gf, home_ga, home_played = extract(home_stats)
        away_gf, away_ga, away_played = extract(away_stats)

        score = 0
        raisons = []
        vigilances = []

        # Potentiel de buts
        expected_home = (home_gf + away_ga) / 2
        expected_away = (away_gf + home_ga) / 2

        if expected_home >= 1.35 and expected_away >= 1.35:
            score += 30
            raisons.append("Fort potentiel de buts des deux côtés")
        elif expected_home >= 1.10 and expected_away >= 1.10:
            score += 22
            raisons.append("Bon potentiel de buts des deux côtés")
        elif expected_home >= 0.90 and expected_away >= 0.90:
            score += 12
        else:
            vigilances.append("Au moins une équipe marque peu")

        # Les deux équipes marquent régulièrement
        if home_gf >= 1.0 and away_gf >= 1.0:
            score += 20
            raisons.append("Les deux équipes marquent régulièrement")
        elif home_gf >= 0.8 and away_gf >= 0.8:
            score += 10
        else:
            vigilances.append("Une attaque est un peu faible")

        # Défenses perméables
        if home_ga >= 1.0 and away_ga >= 1.0:
            score += 15
            raisons.append("Les deux défenses encaissent régulièrement")
        elif home_ga >= 0.8 and away_ga >= 0.8:
            score += 8

        # H2H
        h2h = get_h2h(home["id"], away["id"])
        time.sleep(0.3)

        if h2h:
            btts_h2h = 0
            matchs_h2h = 0

            for match in h2h:
                goals = match.get("goals", {})
                gh = goals.get("home")
                ga = goals.get("away")

                if gh is None or ga is None:
                    continue

                matchs_h2h += 1

                if gh > 0 and ga > 0:
                    btts_h2h += 1

            if matchs_h2h >= 3:
                pct = btts_h2h / matchs_h2h

                if pct >= 0.65:
                    score += 10
                    raisons.append("H2H souvent BTTS")
                elif pct >= 0.45:
                    score += 5

        # Forme récente : seulement si le match est déjà viable
        if score >= 45:
            home_form = get_team_form(home["id"], season)
            time.sleep(0.3)

            away_form = get_team_form(away["id"], season)
            time.sleep(0.3)

            if home_form["matchs"] >= 3 and away_form["matchs"] >= 3:
                home_btts_rate = home_form["btts"] / home_form["matchs"]
                away_btts_rate = away_form["btts"] / away_form["matchs"]
                avg_btts_form = (home_btts_rate + away_btts_rate) / 2

                if avg_btts_form >= 0.70:
                    score += 15
                    raisons.append("Forme récente très favorable au BTTS")
                elif avg_btts_form >= 0.50:
                    score += 8
                elif avg_btts_form <= 0.25:
                    score -= 5
                    vigilances.append("Forme récente peu favorable au BTTS")

        # Blessures
        injuries = get_injuries(fixture_id)
        time.sleep(0.3)

        if injuries:
            missing_home = 0
            missing_away = 0

            for injury in injuries:
                team_id = injury.get("team", {}).get("id")

                if team_id == home["id"]:
                    missing_home += 1
                elif team_id == away["id"]:
                    missing_away += 1

            if missing_home >= 4 or missing_away >= 4:
                score -= 5
                vigilances.append("Plusieurs absences importantes")

        score = max(0, min(100, round(score)))

        raison = raisons[0] if raisons else "Profil statistique correct pour le BTTS"
        vigilance = " ; ".join(vigilances)

        return score, raison, vigilance

    except Exception as e:
        print(f"Erreur calcul: {e}")
        return 0, "Erreur de calcul", ""


# ====================== ANALYSE ======================
def analyse_du_jour():
    print(f"[{datetime.now(PARIS_TZ)}] Lancement du bot BTTS")
    print(f"API_KEY présente: {'Oui' if API_KEY else 'NON'}")

    fixtures = get_fixtures_today()

    if not fixtures:
        send_message(
            "❌ Aucun match trouvé aujourd’hui.\n\n"
            "Causes possibles :\n"
            "• Clé API invalide\n"
            "• Quota API atteint\n"
            "• Aucun match dans les championnats sélectionnés"
        )
        return

    results = []
    scores_debug = []

    for fixture in fixtures:
        score, raison, vigilance = calculate_btts_score(fixture)

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        match_name = f"{home} - {away}"

        scores_debug.append(f"{match_name}: {score}%")

        if score >= MIN_SCORE:
            heure = datetime.fromisoformat(
                fixture["fixture"]["date"].replace("Z", "+00:00")
            ).astimezone(PARIS_TZ).strftime("%H:%M")

            results.append({
                "match": match_name,
                "heure": heure,
                "score": score,
                "raison": raison,
                "vigilance": vigilance,
                "league": fixture["league"]["name"]
            })

        time.sleep(0.2)

    results = sorted(results, key=lambda x: x["score"], reverse=True)[:MAX_ENVOIS]

    today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")

    if not results:
        debug = "\n".join(scores_debug[:15])
        send_message(
            f"⚽ Aucun BTTS ≥ {MIN_SCORE}% aujourd’hui.\n\n"
            f"Scores calculés :\n{debug}"
        )
        return

    message = f"⚽ BTTS OUI — {today} — FILTRE {MIN_SCORE}%\n\n"

    for i, r in enumerate(results, 1):
        message += f"{i}. {r['match']} — {r['heure']} — {r['score']}%\n"
        message += f"✅ {r['raison']}\n"

        if r["vigilance"]:
            message += f"⚠️ {r['vigilance']}\n"

        message += "\n"

        save_prediction(
            datetime.now(PARIS_TZ).strftime("%Y-%m-%d"),
            r["match"],
            r["league"],
            r["score"]
        )

    send_message(message.strip())
    print("Analyse terminée.")


# ====================== TELEGRAM ======================
def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        response = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text
        }, timeout=10)

        print(f"Telegram: {response.status_code}")

        if response.status_code != 200:
            print(f"Erreur Telegram: {response.text}")

    except Exception as e:
        print(f"Erreur Telegram: {e}")


if __name__ == "__main__":
    init_db()
    analyse_du_jour()
