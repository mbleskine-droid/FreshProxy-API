#!/usr/bin/env python3
"""
Proxy Scraper & Checker v6.2 — PURE SPEED + CroxyProxy Fallback
================================================================
NOUVEAU v6.2:
  - Step 1 fetch: GitHub direct → fallback CroxyProxy si 429/erreur
  - CroxyProxy API: https://mb-idk-croxyproxyapi.hf.space/
  - Flag --once pour mode API (une seule itération)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import os
import ssl
import time
import re
import threading
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, Tuple, List, FrozenSet, Optional
from enum import IntEnum
from collections import defaultdict

import aiohttp
import aiofiles

try:
    from aiohttp_socks import ProxyConnector
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

try:
    import uvloop
    uvloop.install()
    LOOP_ENGINE = "uvloop"
except ImportError:
    LOOP_ENGINE = "asyncio"

try:
    import orjson
    def json_dumps(obj): return orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode()
    def json_loads(s):    return orjson.loads(s) if isinstance(s, (bytes, str)) else s
    JSON_ENGINE = "orjson"
except ImportError:
    import json
    def json_dumps(obj): return json.dumps(obj, indent=2, ensure_ascii=False)
    def json_loads(s):    return json.loads(s) if isinstance(s, str) else s
    JSON_ENGINE = "json"


# ══════════════════════════════════════════════
#  PLATFORM DETECTION
# ══════════════════════════════════════════════

IS_WINDOWS = sys.platform == "win32"

# Config identique Windows/Linux (optimisée pour Render free tier)
TCP_CONCURRENCY   = 200
HTTP_CONCURRENCY  = 80
TCP_BATCH_SIZE    = 300
HTTP_BATCH_SIZE   = 100
FETCH_CONCURRENCY = 10


# ══════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════

CROXY_API_BASE   = "https://mb-idk-croxyproxyapi.hf.space"
CROXY_FETCH_URL  = f"{CROXY_API_BASE}/proxy/fetch"
CROXY_HEALTH_URL = f"{CROXY_API_BASE}/health"

RATE_LIMIT_RETRY_DELAY = 5.0

PROXY_SOURCES: Tuple[Tuple[str, str, str], ...] = (
    ("http",   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",   "http"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("http",   "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/protocols/http/data.txt",   "http"),
    ("socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/protocols/socks4/data.txt", "socks4"),
    ("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/protocols/socks5/data.txt", "socks5"),
    ("http",   "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "http"),
    ("http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",   "http"),
    ("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt", "socks4"),
    ("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt", "socks5"),
    ("http",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",   "http"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "socks5"),
)

ANON_SERVICES: List[str] = [
    "https://httpbun.com/get",
    "https://httpbin.org/get",
]

IP_SERVICES: List[Tuple[str, Optional[str]]] = [
    ("https://api.ipify.org?format=json", "ip"),
    ("https://httpbun.com/ip",            "origin"),
    ("https://ifconfig.me/ip",            None),
    ("https://icanhazip.com",             None),
    ("https://ident.me",                  None),
]

TCP_TIMEOUT      = 2.0
HTTP_TIMEOUT     = 6.0
CROXY_TIMEOUT    = 45.0

LOOP_INTERVAL    = 90
DEAD_TTL_SECONDS = 300

OUTPUT_DIR       = "output"
PERSISTENT_FILE  = os.path.join(OUTPUT_DIR, "VERIFIED_PROXIES.txt")
PERSISTENT_ELITE = os.path.join(OUTPUT_DIR, "VERIFIED_ELITE.txt")
PERSISTENT_JSON  = os.path.join(OUTPUT_DIR, "VERIFIED_DETAILED.json")
DEAD_LIST_FILE   = os.path.join(OUTPUT_DIR, "DEAD_PROXIES.json")

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json,text/html,*/*;q=0.8",
}


# ══════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════

class Anon(IntEnum):
    UNKNOWN     = 0
    TRANSPARENT = 1
    ANONYMOUS   = 2
    ELITE       = 3


@dataclass(slots=True)
class Proxy:
    ip: str
    port: int
    source: str     = "http"
    sources: str    = ""
    alive: bool     = False
    country: str    = ""
    proxy_type: str = ""
    anonymity: Anon = Anon.UNKNOWN
    ms: float       = 0.0
    leaked: Tuple[str, ...] = ()
    exit_ip: str    = ""

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def scheme(self) -> str:
        s = (self.proxy_type or self.source or "http").lower().strip()
        return "http" if s in ("http", "https") else s

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.ip}:{self.port}"

    def __hash__(self):  return hash((self.ip, self.port))
    def __eq__(self, o): return isinstance(o, Proxy) and self.ip == o.ip and self.port == o.port


LEAK_HEADERS: FrozenSet[str] = frozenset({
    "x-forwarded-for", "x-real-ip", "via", "forwarded",
    "x-forwarded", "forwarded-for", "x-forwarded-proto",
    "x-forwarded-host", "x-originating-ip", "x-remote-ip",
    "x-remote-addr", "x-proxy-id", "proxy-connection",
    "client-ip", "true-client-ip", "x-client-ip",
    "cf-connecting-ip", "x-bluecoat-via", "x-proxy-connection",
})

LEAK_DISPLAY: Dict[str, str] = {h.lower(): h for h in [
    "X-Forwarded-For", "X-Real-Ip", "Via", "Forwarded", "X-Forwarded",
    "Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host",
    "X-Originating-Ip", "X-Remote-Ip", "X-Remote-Addr", "X-Proxy-Id",
    "Proxy-Connection", "Client-Ip", "True-Client-Ip", "X-Client-Ip",
    "Cf-Connecting-Ip", "X-Bluecoat-Via", "X-Proxy-Connection",
]}


# ══════════════════════════════════════════════
#  ECHO ROUND-ROBIN
# ══════════════════════════════════════════════

class EchoRotator:
    __slots__ = ('_services', '_idx', '_lock')

    def __init__(self, services: List[str]):
        self._services = services
        self._idx      = 0
        self._lock     = threading.Lock()

    def next(self) -> str:
        with self._lock:
            svc = self._services[self._idx % len(self._services)]
            self._idx += 1
        return svc

echo_rotator = EchoRotator(ANON_SERVICES)


# ══════════════════════════════════════════════
#  TERMINAL
# ══════════════════════════════════════════════

class C:
    R  = "\033[91m"; G  = "\033[92m"; Y  = "\033[93m"
    B  = "\033[94m"; M  = "\033[95m"; CY = "\033[96m"
    W  = "\033[97m"; BD = "\033[1m";  DM = "\033[2m"; X = "\033[0m"


def banner():
    plat = "Windows/Proactor" if IS_WINDOWS else "Linux/epoll"
    print(f"""{C.CY}{C.BD}
    ╔════════════════════════════════════════════════════════════════╗
    ║   ⚡  PROXY SCRAPER v6.2 — PURE SPEED + CROXY FALLBACK    ⚡  ║
    ║                                                                ║
    ║   🚀 TCP {TCP_CONCURRENCY:>4} / HTTP {HTTP_CONCURRENCY:>3} concurrent  ({plat:<18})  ║
    ║   🚀 {LOOP_ENGINE} · {JSON_ENGINE} · GitHub direct → CroxyProxy fallback  ║
    ╚════════════════════════════════════════════════════════════════╝
    {C.X}""")


def section(step: int, title: str) -> float:
    print(f"\n{C.BD}{C.CY}{'━'*70}\n  ÉTAPE {step} │ {title}\n{'━'*70}{C.X}")
    return time.perf_counter()


# ══════════════════════════════════════════════
#  DEAD LIST
# ══════════════════════════════════════════════

class DeadList:
    def __init__(self):
        self.entries: Dict[str, Dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        if not os.path.exists(DEAD_LIST_FILE):
            return
        try:
            with open(DEAD_LIST_FILE, "rb") as f:
                raw = json_loads(f.read())
            if isinstance(raw, dict):
                self.entries = raw
            elif isinstance(raw, list):
                for e in raw:
                    a = e.get("proxy", "")
                    if a:
                        self.entries[a] = e
            purged = self._purge()
            print(f"  {C.DM}💀 Dead list: {len(self.entries)} ({purged} expirées){C.X}")
        except Exception as e:
            print(f"  {C.R}⚠ Dead list error: {e}{C.X}")
            self.entries = {}

    def _purge(self) -> int:
        if DEAD_TTL_SECONDS <= 0:
            return 0
        now = time.time()
        expired = [
            a for a, e in self.entries.items()
            if now - e.get("ts", 0) > DEAD_TTL_SECONDS
        ]
        for a in expired:
            del self.entries[a]
        if expired:
            self._dirty = True
        return len(expired)

    def mark_dead_batch(self, proxies: List[Proxy], reason: str):
        now = time.time()
        for p in proxies:
            a = p.addr
            if a in self.entries:
                self.entries[a]["fail_count"] = self.entries[a].get("fail_count", 0) + 1
                self.entries[a]["ts"]     = now
                self.entries[a]["reason"] = reason
            else:
                self.entries[a] = {
                    "proxy": a, "ip": p.ip, "port": p.port,
                    "reason": reason, "fail_count": 1, "ts": now,
                }
        self._dirty = True

    def is_dead(self, addr: str) -> bool:
        if DEAD_TTL_SECONDS <= 0:
            return False
        if addr not in self.entries:
            return False
        if time.time() - self.entries[addr].get("ts", 0) > DEAD_TTL_SECONDS:
            del self.entries[addr]
            self._dirty = True
            return False
        return True

    def filter(self, proxies: List[Proxy]) -> Tuple[List[Proxy], int]:
        self._purge()
        out, ex = [], 0
        for p in proxies:
            if self.is_dead(p.addr):
                ex += 1
            else:
                out.append(p)
        return out, ex

    def remove(self, addr: str):
        if addr in self.entries:
            del self.entries[addr]
            self._dirty = True

    def stats(self) -> Dict[str, int]:
        self._purge()
        s: Dict[str, int] = defaultdict(int)
        for e in self.entries.values():
            s[e.get("reason", "?")] += 1
        s["total"] = len(self.entries)
        return dict(s)

    async def save(self):
        if not self._dirty:
            return
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        async with aiofiles.open(DEAD_LIST_FILE, "w") as f:
            await f.write(json_dumps(self.entries))
        self._dirty = False


# ══════════════════════════════════════════════
#  PROXY STORE
# ══════════════════════════════════════════════

class ProxyStore:
    __slots__ = ('verified', '_dirty')

    def __init__(self):
        self.verified: Dict[str, Dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        if not os.path.exists(PERSISTENT_JSON):
            return
        try:
            with open(PERSISTENT_JSON, "rb") as f:
                data = json_loads(f.read())
            self.verified = {e["proxy"]: e for e in data}
            print(f"  {C.DM}📦 Store: {len(self.verified)} en cache{C.X}")
        except Exception:
            pass

    def add(self, proxy: Proxy):
        self.verified[proxy.addr] = {
            "proxy": proxy.addr, "ip": proxy.ip, "port": proxy.port,
            "source": proxy.source, "sources": proxy.sources,
            "anonymity": Anon(proxy.anonymity).name.lower(),
            "country": proxy.country, "type": proxy.proxy_type,
            "response_time_ms": proxy.ms,
            "exit_ip": proxy.exit_ip,
            "leaked_headers": list(proxy.leaked),
            "last_checked": datetime.now().isoformat(),
        }
        self._dirty = True

    def remove(self, addr: str):
        if addr in self.verified:
            del self.verified[addr]
            self._dirty = True

    async def save(self):
        if not self._dirty:
            return
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        entries = sorted(self.verified.values(), key=lambda e: (
            0 if e["anonymity"] == "elite" else 1,
            e.get("response_time_ms") or 99999,
        ))
        async with aiofiles.open(PERSISTENT_JSON, "w") as f:
            await f.write(json_dumps(entries))
        async with aiofiles.open(PERSISTENT_FILE, "w") as f:
            await f.write('\n'.join(e['proxy'] for e in entries) + '\n')
        elite = [e['proxy'] for e in entries if e["anonymity"] == "elite"]
        async with aiofiles.open(PERSISTENT_ELITE, "w") as f:
            await f.write('\n'.join(elite) + '\n' if elite else '')
        self._dirty = False
        print(f"  {C.G}💾 Saved: {len(entries)} verified ({len(elite)} elite){C.X}")

    @property
    def count(self) -> int:
        return len(self.verified)

    @property
    def elite_count(self) -> int:
        return sum(1 for e in self.verified.values() if e["anonymity"] == "elite")

    @property
    def anon_count(self) -> int:
        return sum(1 for e in self.verified.values() if e["anonymity"] == "anonymous")


# ══════════════════════════════════════════════
#  ÉTAPE 1 : FETCH + DEDUP
#  Stratégie : GitHub direct → fallback CroxyProxy si 429/erreur
# ══════════════════════════════════════════════

_RE_BARE = re.compile(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$')
_RE_URL  = re.compile(
    r'^(?:https?|socks[45])://(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$', re.I
)

_croxy_sem = asyncio.Semaphore(3)
_croxy_available: Optional[bool] = None


async def _check_croxy_health(session: aiohttp.ClientSession) -> bool:
    global _croxy_available
    try:
        async with session.get(
            CROXY_HEALTH_URL,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                _croxy_available = data.get("status") == "ready"
                if _croxy_available:
                    print(f"  {C.G}✓ CroxyProxy HF Space: UP "
                          f"({data.get('servers', '?')} servers){C.X}")
                else:
                    print(f"  {C.Y}⚠ CroxyProxy HF Space: not ready{C.X}")
            else:
                _croxy_available = False
                print(f"  {C.R}✗ CroxyProxy HF Space: HTTP {resp.status}{C.X}")
    except Exception as e:
        _croxy_available = False
        print(f"  {C.R}✗ CroxyProxy HF Space: {str(e)[:60]}{C.X}")
    return bool(_croxy_available)


async def _fetch_via_croxy(
    session: aiohttp.ClientSession,
    url: str,
) -> Optional[str]:
    async with _croxy_sem:
        try:
            timeout = aiohttp.ClientTimeout(total=CROXY_TIMEOUT)
            async with session.post(
                CROXY_FETCH_URL,
                json={"url": url, "raw_headers": False},
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json(content_type=None)

                if not data.get("success"):
                    return None

                body = data.get("body")

                if isinstance(body, str):
                    return body

                if isinstance(body, dict):
                    preview = body.get("_preview", "")
                    full    = body.get("_full", "")
                    candidate = full or preview
                    if candidate and _RE_BARE.search(candidate):
                        return candidate

                return None

        except Exception:
            return None


def _parse_proxy_lines(text: str, label: str) -> List[Tuple[str, int, str]]:
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = _RE_BARE.match(line) or _RE_URL.match(line)
        if m:
            results.append((m.group(1), int(m.group(2)), label))
    return results


async def _fetch_one(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
    label: str,
) -> List[Tuple[str, int, str]]:
    need_fallback = False
    direct_was_429 = False

    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=12)
        ) as resp:

            if resp.status == 200:
                text = await resp.text()
                results = _parse_proxy_lines(text, label)
                print(f"  {C.G}✓ {name:>7}: {len(results):>5} "
                      f"{C.DM}[direct]{C.X}")
                return results

            elif resp.status == 429:
                retry_after = resp.headers.get("Retry-After", "?")
                print(f"  {C.Y}⚡ {name:>7}: 429 "
                      f"(Retry-After: {retry_after}) → CroxyProxy{C.X}")
                need_fallback    = True
                direct_was_429   = True

            elif resp.status in (403, 404, 503, 504):
                print(f"  {C.Y}⚡ {name:>7}: HTTP {resp.status} → CroxyProxy{C.X}")
                need_fallback = True

            else:
                print(f"  {C.R}✗ {name:>7}: HTTP {resp.status}{C.X}")
                return []

    except asyncio.TimeoutError:
        print(f"  {C.Y}⚡ {name:>7}: timeout → CroxyProxy{C.X}")
        need_fallback = True

    except aiohttp.ClientConnectionError:
        print(f"  {C.Y}⚡ {name:>7}: connexion erreur → CroxyProxy{C.X}")
        need_fallback = True

    except Exception as e:
        print(f"  {C.R}✗ {name:>7}: {str(e)[:50]}{C.X}")
        return []

    if not need_fallback:
        return []

    if not _croxy_available:
        print(f"  {C.R}✗ {name:>7}: CroxyProxy indispo, source ignorée{C.X}")
        return []

    if direct_was_429:
        await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)

    text = await _fetch_via_croxy(session, url)
    if text:
        results = _parse_proxy_lines(text, label)
        print(f"  {C.CY}↩ {name:>7}: {len(results):>5} "
              f"{C.DM}[via CroxyProxy]{C.X}")
        return results
    else:
        print(f"  {C.R}✗ {name:>7}: CroxyProxy échec aussi{C.X}")
        return []


def _dedup(
    raw: List[Tuple[str, int, str]]
) -> Tuple[List[Proxy], Dict[str, int], int]:
    grouped: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for ip, port, src in raw:
        grouped[(ip, port)].append(src)

    PRI = {"socks5": 3, "socks4": 2, "http": 1}
    proxies, stats, dups = [], defaultdict(int), 0

    for (ip, port), srcs in grouped.items():
        uniq = sorted(set(srcs))
        if len(uniq) > 1:
            dups += 1
        best = max(uniq, key=lambda s: PRI.get(s, 0))
        proxies.append(Proxy(ip=ip, port=port, source=best, sources="+".join(uniq)))
        for s in uniq:
            stats[s] += 1

    return proxies, dict(stats), dups


async def step1_fetch(dead: DeadList) -> Tuple[List[Proxy], int]:
    t0 = section(1, "Fetch + Dedup  [GitHub direct → CroxyProxy fallback]")

    raw: List[Tuple[str, int, str]] = []
    connector = aiohttp.TCPConnector(limit=FETCH_CONCURRENCY, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=connector) as session:

        print(f"  {C.DM}🔍 Ping CroxyProxy HF Space ({CROXY_API_BASE})...{C.X}")
        await _check_croxy_health(session)
        print()

        tasks = [
            _fetch_one(session, n, u, l)
            for n, u, l in PROXY_SOURCES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                raw.extend(r)

    sources_ok     = sum(1 for r in results if isinstance(r, list) and r)
    sources_empty  = sum(1 for r in results if isinstance(r, list) and not r)

    proxies, stats, dups = _dedup(raw)

    print(f"\n  {C.BD}📊 Dedup:{C.X} {len(raw)} brut → "
          f"{len(proxies)} uniques ({dups} doublons)")
    print(f"     Sources OK: {sources_ok} / vides/erreur: {sources_empty}")
    for proto in ("http", "socks4", "socks5"):
        if proto in stats:
            print(f"     {proto}: {stats[proto]}", end="")
    print()

    total_before = len(proxies)
    proxies, excluded = dead.filter(proxies)
    if excluded:
        print(f"  {C.Y}💀 Dead list exclut: {excluded}{C.X}")

    elapsed = time.perf_counter() - t0
    print(f"\n  {C.G}✓ {len(proxies)} à tester{C.X} {C.DM}({elapsed:.1f}s){C.X}")
    return proxies, total_before


# ══════════════════════════════════════════════
#  ÉTAPE 2 : TCP CONNECT — BATCHED GATHER
# ══════════════════════════════════════════════

async def _tcp_check(sem: asyncio.Semaphore, ip: str, port: int) -> bool:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=TCP_TIMEOUT
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False


async def step2_tcp(proxies: List[Proxy], dead: DeadList) -> List[Proxy]:
    t0 = section(2, f"TCP Connect ({len(proxies)} proxies, "
                    f"batch={TCP_BATCH_SIZE}, sem={TCP_CONCURRENCY})")

    sem       = asyncio.Semaphore(TCP_CONCURRENCY)
    alive_all: List[Proxy] = []
    dead_all:  List[Proxy] = []
    total     = len(proxies)
    processed = 0

    for batch_start in range(0, total, TCP_BATCH_SIZE):
        batch   = proxies[batch_start : batch_start + TCP_BATCH_SIZE]
        tasks   = [_tcp_check(sem, p.ip, p.port) for p in batch]
        results = await asyncio.gather(*tasks)

        for proxy, ok in zip(batch, results):
            if ok:
                alive_all.append(proxy)
            else:
                dead_all.append(proxy)

        processed += len(batch)
        pct = processed * 100 / total
        print(f"  {C.CY}⚡ {processed:>5}/{total} ({pct:5.1f}%) │ "
              f"{C.G}{len(alive_all)} alive{C.X} │ "
              f"{C.R}{len(dead_all)} dead{C.X}")

    if dead_all:
        dead.mark_dead_batch(dead_all, "dead_tcp")

    elapsed = time.perf_counter() - t0
    rate = total / elapsed if elapsed > 0 else 0
    print(f"\n  {C.BD}TCP: {C.G}{len(alive_all)} alive{C.X} / "
          f"{C.R}{len(dead_all)} dead{C.X} "
          f"{C.DM}({elapsed:.1f}s — {rate:.0f}/s){C.X}")

    return alive_all


# ══════════════════════════════════════════════
#  ÉTAPE 3 : HTTP ANONYMITY — BATCHED
# ══════════════════════════════════════════════

async def _http_anon_one(
    proxy: Proxy,
    real_ip: str,
    sem: asyncio.Semaphore,
    ssl_ctx: ssl.SSLContext,
) -> Proxy:
    async with sem:
        url    = echo_rotator.next()
        scheme = proxy.scheme
        try:
            if scheme in ("socks4", "socks5") and HAS_SOCKS:
                connector = ProxyConnector.from_url(proxy.url, ssl=ssl_ctx)
            elif scheme in ("socks4", "socks5"):
                proxy.alive  = False
                proxy.leaked = ("SKIP: aiohttp-socks missing",)
                return proxy
            else:
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)

            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            start   = time.perf_counter()

            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                kwargs: Dict = {"headers": HEADERS}
                if scheme in ("http", "https"):
                    kwargs["proxy"] = proxy.url

                async with session.get(url, **kwargs) as resp:
                    elapsed = (time.perf_counter() - start) * 1000
                    if resp.status != 200:
                        proxy.alive  = False
                        proxy.leaked = (f"HTTP {resp.status}",)
                        return proxy
                    data = await resp.json(content_type=None)

            proxy.ms = round(elapsed, 1)

            headers_resp = data.get("headers", {})
            origin       = data.get("origin", "")
            proxy.exit_ip = origin.split(",")[0].strip()

            headers_lower = {k.lower(): v for k, v in headers_resp.items()}
            ip_leaked     = bool(real_ip and real_ip in origin)
            proxy_detected = False
            leaked: List[str] = []

            for hl in LEAK_HEADERS:
                if hl in headers_lower:
                    val = headers_lower[hl]
                    leaked.append(f"{LEAK_DISPLAY.get(hl, hl)}: {val}")
                    if real_ip and real_ip in str(val):
                        ip_leaked = True
                    else:
                        proxy_detected = True

            proxy.leaked = tuple(leaked)

            if ip_leaked:
                proxy.anonymity = Anon.TRANSPARENT
            elif proxy_detected:
                proxy.anonymity = Anon.ANONYMOUS
            else:
                proxy.anonymity = Anon.ELITE

            proxy.alive = True

        except Exception as e:
            proxy.alive     = False
            proxy.anonymity = Anon.UNKNOWN
            proxy.leaked    = (f"ERR {type(e).__name__}: {str(e)[:100]}",)

    return proxy


async def _detect_real_ip() -> str:
    connector = aiohttp.TCPConnector(limit=5)
    timeout   = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for url, key in IP_SERVICES:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    if key is None:
                        return (await resp.text()).strip().split(",")[0].strip()
                    data = await resp.json(content_type=None)
                    return str(data.get(key, "")).split(",")[0].strip()
            except Exception:
                continue
    return ""


async def step3_anonymity(
    proxies: List[Proxy],
    dead: DeadList,
    store: ProxyStore,
) -> List[Proxy]:
    t0 = section(3, f"HTTP Anonymity ({len(proxies)} proxies, "
                    f"batch={HTTP_BATCH_SIZE}, sem={HTTP_CONCURRENCY})")

    print(f"  {C.CY}🔍 Détection IP...{C.X}", end=" ", flush=True)
    real_ip = await _detect_real_ip()
    if not real_ip:
        print(f"{C.R}ÉCHEC{C.X}")
        return []
    print(f"{C.Y}{real_ip}{C.X}")
    print(f"  {C.DM}Echo: {', '.join(ANON_SERVICES)}{C.X}\n")

    sem     = asyncio.Semaphore(HTTP_CONCURRENCY)
    ssl_ctx = ssl.create_default_context()

    all_results: List[Proxy] = []
    total     = len(proxies)
    processed = 0
    counts    = {"elite": 0, "anon": 0, "trans": 0, "dead": 0}

    for batch_start in range(0, total, HTTP_BATCH_SIZE):
        batch = proxies[batch_start : batch_start + HTTP_BATCH_SIZE]
        tasks = [_http_anon_one(p, real_ip, sem, ssl_ctx) for p in batch]

        for coro in asyncio.as_completed(tasks):
            result     = await coro
            processed += 1
            all_results.append(result)

            if result.anonymity == Anon.ELITE:
                counts["elite"] += 1
                icon = f"{C.G}★ ELITE{C.X}"
            elif result.anonymity == Anon.ANONYMOUS:
                counts["anon"]  += 1
                icon = f"{C.Y}● ANON {C.X}"
            elif result.anonymity == Anon.TRANSPARENT:
                counts["trans"] += 1
                icon = f"{C.R}✗ TRANS{C.X}"
            else:
                counts["dead"]  += 1
                icon = f"{C.DM}? DEAD {C.X}"

            speed = f"{result.ms:.0f}ms" if result.ms else "fail"
            show  = (
                result.anonymity >= Anon.ANONYMOUS
                or processed % 100 == 0
                or processed == total
            )

            if show:
                err = ""
                if result.anonymity == Anon.UNKNOWN and result.leaked:
                    raw_e = result.leaked[0]
                    if "Timeout" in raw_e:             err = f" {C.DM}[timeout]{C.X}"
                    elif "SOCKS" in raw_e:             err = f" {C.R}[socks]{C.X}"
                    elif "refused" in raw_e.lower():   err = f" {C.DM}[refused]{C.X}"
                    elif "SKIP"    in raw_e:           err = f" {C.Y}[no lib]{C.X}"
                    else:                              err = f" {C.DM}[{raw_e[:25]}]{C.X}"

                print(f"  {icon} {result.addr:<22} {speed:>7} "
                      f"[{processed}/{total}]{err}")

    transparent  = [p for p in all_results if p.anonymity == Anon.TRANSPARENT]
    dead_proxies = [p for p in all_results if p.anonymity == Anon.UNKNOWN]

    for p in transparent + dead_proxies:
        if p.addr in store.verified:
            store.remove(p.addr)

    if transparent:
        dead.mark_dead_batch(transparent, "transparent")
    if dead_proxies:
        dead.mark_dead_batch(dead_proxies, "dead_http")

    elapsed = time.perf_counter() - t0
    rate    = total / elapsed if elapsed > 0 else 0

    print(f"\n  {'─' * 55}")
    print(f"  {C.G}★ Elite      : {counts['elite']}{C.X}")
    print(f"  {C.Y}● Anonymous  : {counts['anon']}{C.X}")
    print(f"  {C.R}✗ Transparent: {counts['trans']}{C.X}")
    print(f"  {C.DM}? Dead       : {counts['dead']}{C.X}")
    print(f"  {C.DM}⏱  {elapsed:.1f}s ({rate:.0f}/s){C.X}")

    final = [p for p in all_results if p.anonymity >= Anon.ANONYMOUS]
    final.sort(key=lambda p: (0 if p.anonymity == Anon.ELITE else 1, p.ms or 99999))
    return final


# ══════════════════════════════════════════════
#  RÉSUMÉ
# ══════════════════════════════════════════════

def print_summary(
    store: ProxyStore,
    dead: DeadList,
    new: List[Proxy],
    iteration: int,
    total_raw: int,
    pass_tcp: int,
    pass_http: int,
    total_time: float,
):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{C.BD}{C.CY}{'━' * 70}")
    print(f"  RÉSUMÉ │ #{iteration} à {now} │ {total_time:.1f}s total")
    print(f"{'━' * 70}{C.X}")

    print(f"\n  {C.BD}Pipeline:{C.X}")
    print(f"     Uniques         : {total_raw}")
    print(f"  {C.CY}  → TCP alive       : {pass_tcp}{C.X}")
    print(f"  {C.G}  → HTTP vérifié    : {pass_http}{C.X}")

    if new:
        print(f"\n  {C.BD}Top 15:{C.X}")
        for p in new[:15]:
            anon  = f"{C.G}★ ELITE" if p.anonymity == Anon.ELITE else f"{C.Y}● ANON"
            speed = f"{p.ms:.0f}ms" if p.ms else "N/A"
            print(f"    {p.addr:<22} {anon}{C.X} {speed:>7} [{p.scheme}]")

    ds = dead.stats()
    if ds.get("total", 0):
        print(f"\n  {C.BD}💀 Dead list:{C.X} {ds['total']}")
        for r in ("dead_tcp", "dead_http", "transparent"):
            if ds.get(r, 0):
                print(f"     {r:<18}: {ds[r]}")

    print(f"\n  {'═' * 55}")
    print(f"  {C.BD}{C.G}📊 TOTAL: {store.count} verified "
          f"(★ {store.elite_count} elite │ ● {store.anon_count} anon){C.X}")
    print(f"  {C.DM}📄 {PERSISTENT_FILE}"
          f"\n  📄 {PERSISTENT_ELITE}"
          f"\n  📄 {PERSISTENT_JSON}{C.X}")


# ══════════════════════════════════════════════
#  ITÉRATION
# ══════════════════════════════════════════════

async def run_iteration(store: ProxyStore, dead: DeadList, iteration: int):
    now     = datetime.now().strftime("%H:%M:%S")
    t_start = time.perf_counter()

    print(f"\n\n{C.BD}{C.M}{'▓' * 70}")
    print(f"  ITÉRATION #{iteration} │ {now} │ "
          f"Store: {store.count} │ Dead: {len(dead.entries)}")
    print(f"{'▓' * 70}{C.X}")

    proxies, total_raw = await step1_fetch(dead)
    if not proxies:
        return

    tcp_alive = await step2_tcp(proxies, dead)
    if not tcp_alive:
        print(f"  {C.R}Aucun survivant TCP{C.X}")
        await dead.save()
        return

    final = await step3_anonymity(tcp_alive, dead, store)

    # ── Vider le store : on ne garde QUE les proxies fraîchement vérifiés ──
    store.verified = {}
    store._dirty   = True

    for p in final:
        store.add(p)
        dead.remove(p.addr)

    await store.save()
    await dead.save()

    total_time = time.perf_counter() - t_start
    print_summary(store, dead, final, iteration,
                  total_raw, len(tcp_alive), len(final), total_time)


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

async def main():
    # ── Parse --once flag pour mode API ──────────────────────────────────
    parser = argparse.ArgumentParser(description="Proxy Scraper v6.2")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Une seule itération puis quit (mode API)"
    )
    args, _ = parser.parse_known_args()

    banner()

    print(f"  {C.BD}Platform:{C.X}")
    print(f"  {'🪟' if IS_WINDOWS else '🐧'} {sys.platform} │ "
          f"Python {sys.version.split()[0]}")
    if IS_WINDOWS:
        print(f"  {C.CY}  ProactorEventLoop (IOCP) — "
              f"TCP {TCP_CONCURRENCY} / HTTP {HTTP_CONCURRENCY}{C.X}")
    else:
        print(f"  {C.G}  epoll — TCP {TCP_CONCURRENCY} / HTTP {HTTP_CONCURRENCY}{C.X}")

    print(f"\n  {C.BD}Deps:{C.X}")
    print(f"  {C.G}✓ aiohttp · aiofiles · {JSON_ENGINE} · {LOOP_ENGINE}{C.X}")
    if HAS_SOCKS:
        print(f"  {C.G}✓ aiohttp-socks (SOCKS support){C.X}")
    else:
        print(f"  {C.Y}⚠ pip install aiohttp-socks  (SOCKS proxies ignorés){C.X}")

    print(f"\n  {C.BD}CroxyProxy fallback:{C.X}")
    print(f"  {C.CY}  API  : {CROXY_API_BASE}{C.X}")
    print(f"  {C.CY}  Sem  : 3 appels simultanés max{C.X}")
    print(f"  {C.CY}  Délai: {RATE_LIMIT_RETRY_DELAY}s après 429 GitHub{C.X}")
    print(f"  {C.CY}  Trigger: 429 · timeout · erreur connexion{C.X}")

    print(f"\n  {C.BD}Speed:{C.X}")
    print(f"  {C.CY}  TCP  : sem={TCP_CONCURRENCY}, batch={TCP_BATCH_SIZE}, "
          f"timeout={TCP_TIMEOUT}s{C.X}")
    print(f"  {C.CY}  HTTP : sem={HTTP_CONCURRENCY}, batch={HTTP_BATCH_SIZE}, "
          f"timeout={HTTP_TIMEOUT}s{C.X}")
    print(f"  {C.CY}  Echo : {len(ANON_SERVICES)} services rotation{C.X}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    store     = ProxyStore()
    dead      = DeadList()

    # ══════════════════════════════════════════════════════════════════
    #  MODE --once : une seule itération (pour api.py / Render)
    # ══════════════════════════════════════════════════════════════════
    if args.once:
        print(f"\n  {C.BD}{C.Y}🔂 Mode --once : une seule itération{C.X}")
        await run_iteration(store, dead, 1)
        print(f"\n  {C.G}✓ Itération terminée. Exiting.{C.X}\n")
        return

    # ══════════════════════════════════════════════════════════════════
    #  MODE NORMAL : boucle infinie
    # ══════════════════════════════════════════════════════════════════
    iteration = 0
    print(f"\n  {C.BD}{C.CY}🔄 Boucle: {LOOP_INTERVAL}s (Ctrl+C stop){C.X}")

    try:
        while True:
            iteration += 1
            t0 = time.perf_counter()
            await run_iteration(store, dead, iteration)
            elapsed = time.perf_counter() - t0
            wait    = max(0, LOOP_INTERVAL - elapsed)
            if wait > 0:
                print(f"\n  {C.DM}⏳ Prochaine dans {int(wait)}s...{C.X}")
                await asyncio.sleep(wait)
            else:
                print(f"\n  {C.Y}⚡ {elapsed:.0f}s, relance immédiate{C.X}")

    except KeyboardInterrupt:
        print(f"\n\n  {C.Y}{C.BD}⚡ Arrêt.{C.X}")
        await dead.save()
        await store.save()
        print(f"\n  {C.G}Final: {store.count} verified "
              f"(★ {store.elite_count} │ ● {store.anon_count}){C.X}")
        print(f"  {C.R}💀 Dead: {len(dead.entries)}{C.X}\n")


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(main())
