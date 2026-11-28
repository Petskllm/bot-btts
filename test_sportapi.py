"""Diagnostic manuel de l’endpoint fixtures SportAPI.

Ce fichier reste compatible avec ``unittest discover``. Il n’effectue aucun
appel réseau au moment de son import : le diagnostic ne s’exécute que via
``python test_sportapi.py``.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")


def main() -> int:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    api_key = os.getenv("API_KEY", "").strip()
    today = datetime.now(PARIS).date().isoformat()
    url = f"{BASE_URL}/fixtures/date/{today}"

    print("DATE :", today)
    print("URL :", url)
    print("CLE PRESENTE :", bool(api_key))

    try:
        response = requests.get(
            url,
            headers={"X-Api-Key": api_key},
            timeout=25,
        )
    except requests.RequestException as exc:
        print("ERREUR RESEAU :", type(exc).__name__, str(exc))
        return 1

    print("STATUT HTTP :", response.status_code)
    try:
        data = response.json()
    except ValueError:
        data = None

    print("REPONSE :", str(data)[:5000])
    if isinstance(data, dict):
        print("CLES :", list(data.keys()))
        matches = (
            data.get("fixtures")
            or data.get("matches")
            or data.get("events")
            or data.get("data")
            or data.get("response")
            or []
        )
    elif isinstance(data, list):
        matches = data
    else:
        matches = []

    print("NOMBRE MATCHS :", len(matches))
    if matches:
        print("PREMIER MATCH :")
        print(str(matches[0])[:5000])
        message = (
            "✅ Test SportAPI : matchs trouvés.\n"
            f"Nombre : {len(matches)}\n\n"
            "Regarde les logs GitHub et copie le premier match."
        )
    else:
        message = (
            "⚠️ Test SportAPI : aucun match détecté.\n"
            f"URL testée : {url}\n"
            f"Statut HTTP : {response.status_code}\n\n"
            "Regarde les logs GitHub."
        )

    # L’envoi est opt-in : un diagnostic local ne doit pas envoyer de message.
    if os.getenv("SEND_TELEGRAM", "").strip().lower() in {"1", "true", "yes"}:
        if not token or not chat_id:
            print("Telegram non configure : message non envoye.")
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=25,
            ).raise_for_status()
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
