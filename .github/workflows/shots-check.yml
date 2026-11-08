# Test de couverture des stats de tirs (SportAPI). ~50 requetes.
import os, re, time, statistics
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("CHAT_ID", "").strip()
KEY = os.getenv("API_KEY", "").strip()
BASE = "https://sportapi.ai/api"
S = requests.Session()
S.headers.update({"X-Api-Key": KEY})
YOUTH = re.compile(r"\bU[- ]?(1[5-9]|2[0-3])\b|youth|primavera|reserve|\bII$", re.I)
calls = 0


def get(path):
    global calls
    if calls >= 70:
        return None
    calls += 1
    time.sleep(0.4)
    try:
        r = S.get(f"{BASE}/{path}", timeout=25)
    except requests.RequestException:
        return None
    if r.status_code in (401, 403):
        raise SystemExit(f"Cle API refusee ({r.status_code})")
    try:
        return r.json() if r.ok else None
    except ValueError:
        return None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def blocks(x, out=None):
    out = [] if out is None else out
    if isinstance(x, dict):
        if num(x.get("shots")) is not None:
            out.append(x)
        for v in x.values():
            blocks(v, out)
    elif isinstance(x, list):
        for v in x:
            blocks(v, out)
    return out


def flag(x):
    if isinstance(x, dict):
        if isinstance(x.get("available"), bool):
            return x["available"]
        x = list(x.values())
    if isinstance(x, list):
        for v in x:
            f = flag(v)
            if f is not None:
                return f
    return None


def send(text):
    if not TOKEN or not CHAT:
        print("Telegram non configure")
        return
    for i in range(0, len(text), 3000):
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": CHAT, "text": text[i:i + 3000]}, timeout=25)
        except requests.RequestException:
            print("Erreur Telegram")


def main():
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    found = {}
    for d in (1, 2, 3):
        p = get(f"fixtures/date/{today - timedelta(days=d)}")
        for m in (p or {}).get("data") or []:
            if (isinstance(m, dict) and str(m.get("status")).upper() == "FT"
                    and m.get("id") is not None
                    and not YOUTH.search(f"{m.get('home_team')} {m.get('away_team')}")):
                found[m["id"]] = m
    print(len(found), "matchs termines")
    if not found:
        return send("TEST TIRS : aucun match termine trouve.")
    by = defaultdict(list)
    for m in found.values():
        by[str(m.get("league_id"))].append(m)
    sample = []
    for r in range(3):
        for k in sorted(by, key=lambda k: -len(by[k])):
            if len(sample) < 45 and r < len(by[k]):
                sample.append(by[k][r])
    rows, shown = [], False
    for m in sample:
        p = get(f"fixtures/{m['id']}/stats")
        if p is not None and not shown:
            shown = True
            print("EXEMPLE:", str(p)[:1500])
        b = blocks(p)[:2] if p is not None else []
        if p is None:
            st, tot, cad = "echec", None, None
        elif len(b) < 2:
            st, tot, cad = ("indisponible" if flag(p) is False else "vide"), None, None
        else:
            tot = b[0]["shots"] + b[1]["shots"]
            c = [num(x.get("shots_on_target")) for x in b]
            cad = sum(c) if None not in c else None
            st = "ok" if tot > 0 or flag(p) else "zero"
        name = str(m.get("league_name") or m.get("league") or f"ligue {m.get('league_id')}")
        rows.append((name, st, tot, cad))
        print(st, name, m.get("home_team"), m.get("away_team"), tot, cad)
    ok = [r for r in rows if r[1] == "ok"]
    n = len(rows)
    L = ["TEST STATS DE TIRS", f"{n} matchs testes, {calls} requetes", "",
         f"Stats de tirs disponibles : {len(ok)}/{n} ({len(ok) / max(n, 1):.0%})"]
    bad = defaultdict(int)
    for r in rows:
        if r[1] != "ok":
            bad[r[1]] += 1
    if bad:
        L.append("Absents : " + ", ".join(f"{k} {v}" for k, v in bad.items()))
    if len(ok) >= 5:
        t = [r[2] for r in ok]
        L += ["", f"Tirs par match : moyenne {statistics.mean(t):.1f}, ecart-type {statistics.pstdev(t):.1f}, min {min(t):.0f}, max {max(t):.0f}",
              "Part des matchs : " + " | ".join(f"{x}+ tirs {sum(v >= x for v in t) / len(t):.0%}" for x in (20, 25, 27, 30))]
        c = [r[3] for r in ok if r[3] is not None]
        if c:
            L.append(f"Tirs cadres par match : moyenne {statistics.mean(c):.1f}")
    lg = defaultdict(lambda: [0, 0])
    for r in rows:
        lg[r[0]][1] += 1
        lg[r[0]][0] += r[1] == "ok"
    L += ["", "Par ligue (dispo/testes) :"]
    for k, (g, t) in sorted(lg.items(), key=lambda kv: -kv[1][1])[:20]:
        L.append(f"- {k[:30]} : {g}/{t}")
    send("\n".join(L))
    print("\n".join(L))


main()
print("FIN DU SCRIPT")
