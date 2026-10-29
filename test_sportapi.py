import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

today = datetime.now(PARIS).date().isoformat()

url = f"{BASE_URL}/fixtures/date/{today}"

print("DATE :", today)
print("URL :", url)
print("CLE PRESENTE :", bool(API_KEY))

response = requests.get(
    url,
    headers={"X-Api-Key": API_KEY or ""},
    timeout=25,
)

print("STATUT HTTP :", response.status_code)

try:
    data = response.json()
except Exception:
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

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message,
    },
    timeout=25,
)

