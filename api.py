"""
Proxy Scraper API — Render wrapper
===================================
Lance le scraper v6.2 en background thread
Expose les résultats via endpoints REST
"""

import os
import json
import time
import threading
import asyncio
import subprocess
from datetime import datetime
from flask import Flask, jsonify, request

app = Flask(__name__)

# ══════════════════════════════════════════════
#  ÉTAT GLOBAL
# ══════════════════════════════════════════════

STATE = {
    "status":        "starting",   # starting | running | idle | error
    "iteration":     0,
    "last_run":      None,
    "next_run":      None,
    "total":         0,
    "elite":         0,
    "anon":          0,
    "dead":          0,
    "last_error":    None,
    "started_at":    datetime.now().isoformat(),
}

OUTPUT_DIR    = "output"
JSON_FILE     = os.path.join(OUTPUT_DIR, "VERIFIED_DETAILED.json")
ELITE_FILE    = os.path.join(OUTPUT_DIR, "VERIFIED_ELITE.txt")
ALL_FILE      = os.path.join(OUTPUT_DIR, "VERIFIED_PROXIES.txt")

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ══════════════════════════════════════════════
#  LECTURE DES FICHIERS OUTPUT
# ══════════════════════════════════════════════

def _load_json() -> list:
    if not os.path.exists(JSON_FILE):
        return []
    try:
        with open(JSON_FILE, "rb") as f:
            return json.loads(f.read())
    except Exception:
        return []


def _load_txt(filepath: str) -> list:
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


# ══════════════════════════════════════════════
#  SCRAPER BACKGROUND THREAD
# ══════════════════════════════════════════════

def _run_scraper_loop():
    """Lance scraper.py en subprocess dans une boucle infinie."""
    global STATE

    # Attendre que Flask soit bien up avant de commencer
    time.sleep(5)

    STATE["status"] = "running"

    while True:
        STATE["iteration"] += 1
        STATE["status"]     = "running"
        STATE["last_run"]   = datetime.now().isoformat()

        try:
            # Lance scraper.py (une seule itération, pas la boucle infinie)
            # Le flag --once est géré dans scraper.py
            result = subprocess.run(
                ["python", "scraper.py", "--once"],
                capture_output=True,
                text=True,
                timeout=600,   # 10 min max par itération
            )

            if result.returncode == 0:
                # Lire les stats depuis les fichiers générés
                proxies = _load_json()
                STATE["total"] = len(proxies)
                STATE["elite"] = sum(1 for p in proxies if p.get("anonymity") == "elite")
                STATE["anon"]  = sum(1 for p in proxies if p.get("anonymity") == "anonymous")
                STATE["status"] = "idle"
                STATE["last_error"] = None
            else:
                STATE["status"]     = "error"
                STATE["last_error"] = result.stderr[-500:] if result.stderr else "unknown"

        except subprocess.TimeoutExpired:
            STATE["status"]     = "error"
            STATE["last_error"] = "Timeout (>10min)"

        except Exception as e:
            STATE["status"]     = "error"
            STATE["last_error"] = str(e)[:200]

        # Pause 90s entre chaque run (même intervalle que le script original)
        STATE["next_run"] = datetime.fromtimestamp(
            time.time() + 90
        ).isoformat()
        STATE["status"] = "idle"
        time.sleep(90)


# Lance le thread au démarrage
_thread = threading.Thread(target=_run_scraper_loop, daemon=True)
_thread.start()


# ══════════════════════════════════════════════
#  KEEP-ALIVE (évite le spin-down Render free)
# ══════════════════════════════════════════════

def _keep_alive():
    time.sleep(60)
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not base_url:
        return
    import urllib.request
    while True:
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)   # ping toutes les 10 min

if os.environ.get("RENDER"):
    threading.Thread(target=_keep_alive, daemon=True).start()


# ══════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════

@app.route("/")
def index():
    return jsonify({
        "name":      "Proxy Scraper API",
        "version":   "1.0",
        "endpoints": {
            "GET /health":              "Status du scraper",
            "GET /proxies":             "Tous les proxies vérifiés (JSON détaillé)",
            "GET /proxies/elite":       "Proxies elite uniquement",
            "GET /proxies/anonymous":   "Proxies anonymous uniquement",
            "GET /proxies/list":        "Liste brute IP:PORT (plain text)",
            "GET /proxies/elite/list":  "Liste elite brute IP:PORT (plain text)",
            "GET /proxies/random":      "1 proxy aléatoire",
            "GET /proxies/random?type=elite":    "1 proxy elite aléatoire",
            "GET /proxies/random?type=anonymous": "1 proxy anon aléatoire",
            "GET /proxies/stats":       "Stats détaillées",
            "GET /proxies/fast":        "Top 20 proxies les plus rapides",
        },
        "params": {
            "/proxies":          "?limit=N  ?type=elite|anonymous|all  ?scheme=http|socks5|socks4",
            "/proxies/random":   "?type=elite|anonymous|all  ?scheme=http|socks5|socks4",
        }
    })


@app.route("/health")
def health():
    return jsonify({
        "status":      STATE["status"],
        "iteration":   STATE["iteration"],
        "last_run":    STATE["last_run"],
        "next_run":    STATE["next_run"],
        "proxies": {
            "total": STATE["total"],
            "elite": STATE["elite"],
            "anon":  STATE["anon"],
        },
        "uptime_since": STATE["started_at"],
        "last_error":   STATE["last_error"],
    })


@app.route("/proxies")
def proxies_all():
    proxies = _load_json()

    # Filtres query params
    ptype  = request.args.get("type",   "all")
    scheme = request.args.get("scheme", "all")
    limit  = int(request.args.get("limit", 0))

    if ptype != "all":
        proxies = [p for p in proxies if p.get("anonymity") == ptype]

    if scheme != "all":
        proxies = [p for p in proxies if p.get("type", "").lower() == scheme]

    if limit > 0:
        proxies = proxies[:limit]

    return jsonify({
        "count":   len(proxies),
        "proxies": proxies,
    })


@app.route("/proxies/elite")
def proxies_elite():
    proxies = [p for p in _load_json() if p.get("anonymity") == "elite"]
    limit   = int(request.args.get("limit", 0))
    scheme  = request.args.get("scheme", "all")

    if scheme != "all":
        proxies = [p for p in proxies if p.get("type", "").lower() == scheme]

    if limit > 0:
        proxies = proxies[:limit]

    return jsonify({"count": len(proxies), "proxies": proxies})


@app.route("/proxies/anonymous")
def proxies_anon():
    proxies = [p for p in _load_json() if p.get("anonymity") == "anonymous"]
    limit   = int(request.args.get("limit", 0))

    if limit > 0:
        proxies = proxies[:limit]

    return jsonify({"count": len(proxies), "proxies": proxies})


@app.route("/proxies/list")
def proxies_list_txt():
    """Retourne une liste brute IP:PORT — pratique pour les scripts."""
    lines = _load_txt(ALL_FILE)
    return "\n".join(lines), 200, {"Content-Type": "text/plain"}


@app.route("/proxies/elite/list")
def proxies_elite_txt():
    """Retourne liste elite brute IP:PORT."""
    lines = _load_txt(ELITE_FILE)
    return "\n".join(lines), 200, {"Content-Type": "text/plain"}


@app.route("/proxies/random")
def proxies_random():
    import random
    proxies = _load_json()

    ptype  = request.args.get("type",   "all")
    scheme = request.args.get("scheme", "all")

    if ptype != "all":
        proxies = [p for p in proxies if p.get("anonymity") == ptype]

    if scheme != "all":
        proxies = [p for p in proxies if p.get("type", "").lower() == scheme]

    if not proxies:
        return jsonify({"error": "no proxy available", "filters": {
            "type": ptype, "scheme": scheme
        }}), 404

    chosen = random.choice(proxies)
    return jsonify({"proxy": chosen})


@app.route("/proxies/fast")
def proxies_fast():
    """Top N proxies triés par response_time_ms."""
    proxies = _load_json()
    n       = int(request.args.get("n", 20))
    ptype   = request.args.get("type", "all")

    if ptype != "all":
        proxies = [p for p in proxies if p.get("anonymity") == ptype]

    proxies = sorted(
        [p for p in proxies if p.get("response_time_ms")],
        key=lambda p: p["response_time_ms"]
    )[:n]

    return jsonify({"count": len(proxies), "proxies": proxies})


@app.route("/proxies/stats")
def proxies_stats():
    proxies = _load_json()

    by_scheme: dict = {}
    by_country: dict = {}
    ms_values = []

    for p in proxies:
        s = p.get("type", "unknown")
        by_scheme[s] = by_scheme.get(s, 0) + 1

        c = p.get("country", "??") or "??"
        by_country[c] = by_country.get(c, 0) + 1

        ms = p.get("response_time_ms")
        if ms:
            ms_values.append(ms)

    avg_ms = round(sum(ms_values) / len(ms_values), 1) if ms_values else 0
    min_ms = round(min(ms_values), 1) if ms_values else 0
    max_ms = round(max(ms_values), 1) if ms_values else 0

    return jsonify({
        "total":       len(proxies),
        "elite":       sum(1 for p in proxies if p.get("anonymity") == "elite"),
        "anonymous":   sum(1 for p in proxies if p.get("anonymity") == "anonymous"),
        "by_scheme":   by_scheme,
        "by_country":  dict(sorted(by_country.items(), key=lambda x: -x[1])[:15]),
        "response_time_ms": {
            "avg": avg_ms,
            "min": min_ms,
            "max": max_ms,
        },
        "scraper": {
            "status":    STATE["status"],
            "iteration": STATE["iteration"],
            "last_run":  STATE["last_run"],
            "next_run":  STATE["next_run"],
        }
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
