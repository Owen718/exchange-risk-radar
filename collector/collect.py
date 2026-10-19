#!/usr/bin/env python3
"""Collect public exchange announcements into a static JSON feed.

Design goals:
  - one process, low memory, no database
  - every source is optional; one failure never blocks the others
  - public APIs only; no HTML scraping, no auth, no trading logic
  - merge with existing JSON so history survives source outages
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "data" / "events.json"
UA = "exchange-risk-radar/0.2 (public announcement monitor)"
TIMEOUT = 15
KST = timezone(timedelta(hours=9))
MAX_EVENTS = 900

TYPE_LABELS = {
    "listing": "上币",
    "delisting": "下架",
    "monitoring_tag": "监控标签",
    "caution": "风险警示",
    "maintenance": "维护/暂停",
    "other": "其他",
}

DELIST_RE = re.compile(
    r"(delist|delisting|removal of|will remove|end of trading|거래\s*지원\s*종료|상장\s*폐지|下架)",
    re.I,
)
CAUTION_RE = re.compile(
    r"(monitoring tag|investment warning|caution|유의\s*종목|투자\s*유의|투자\s*경고|风险警示)",
    re.I,
)
LISTING_RE = re.compile(
    r"(will list|new cryptocurrency listing|market support|new listing|gets listed|initial listing|"
    r"마켓\s*추가|신규\s*거래\s*지원|신규\s*상장|上线|上币)",
    re.I,
)
MAINT_RE = re.compile(
    r"(maintenance|suspension|suspend|outage|degraded|incident|"
    r"입출금|입금|출금|중지|재개|점검|네트워크\s*전환|deposit|withdrawal|暂停|维护)",
    re.I,
)
SYMBOL_RE = re.compile(r"\(([A-Z0-9]{2,15})\)")
QUOTE_ASSETS = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "BTC", "ETH", "BNB", "KRW"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_s(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_bithumb_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def classify(title: str) -> str:
    if CAUTION_RE.search(title) and "monitoring tag" in title.lower():
        return "monitoring_tag"
    if DELIST_RE.search(title):
        return "delisting"
    if CAUTION_RE.search(title):
        return "caution"
    if LISTING_RE.search(title):
        return "listing"
    if MAINT_RE.search(title):
        return "maintenance"
    return "other"


def extract_symbols(title: str) -> list[str]:
    out: list[str] = []
    for raw in SYMBOL_RE.findall(title):
        sym = raw.strip().upper()
        if sym and sym not in QUOTE_ASSETS and sym not in out:
            out.append(sym)
    return out[:8]


def importance(exchange: str, event_type: str) -> int:
    if event_type in ("monitoring_tag", "delisting"):
        return 5 if exchange in ("Binance", "Bithumb", "Upbit") else 4
    if event_type == "listing":
        return 4 if exchange in ("Binance", "Bithumb", "Upbit") else 3
    if event_type == "caution":
        return 4
    if event_type == "maintenance":
        return 2
    return 1


def make_summary(exchange: str, event_type: str, symbols: list[str]) -> str:
    label = TYPE_LABELS.get(event_type, "事件")
    if symbols:
        return f"{exchange} {label}：{', '.join(symbols)}"
    return f"{exchange} {label}公告"


def display_title(exchange: str, event_type: str, symbols: list[str], title: str) -> str:
    """Chinese-first display title; original title stays in `title`."""
    sym = "、".join(symbols)
    if event_type == "listing":
        if exchange == "Bithumb":
            if "원화" in title:
                return f"Bithumb 新增韩元市场：{sym}" if sym else "Bithumb 新增韩元市场"
            if "BTC" in title:
                return f"Bithumb 新增 BTC 市场：{sym}" if sym else "Bithumb 新增 BTC 市场"
            return f"Bithumb 新增市场：{sym}" if sym else "Bithumb 新增市场"
        return f"{exchange} 上币：{sym}" if sym else f"{exchange} 上币公告"
    if event_type == "delisting":
        return f"{exchange} 下架：{sym}" if sym else f"{exchange} 下架公告"
    if event_type == "monitoring_tag":
        return f"{exchange} 监控标签：{sym}" if sym else f"{exchange} 监控标签公告"
    if event_type == "caution":
        return f"{exchange} 风险警示：{sym}" if sym else f"{exchange} 风险警示"
    if event_type == "maintenance":
        if exchange == "Bithumb":
            if "재개" in title:
                return f"Bithumb 充提恢复：{sym}" if sym else "Bithumb 充提恢复"
            if "입출금" in title or "입금" in title or "출금" in title:
                return f"Bithumb 充提暂停：{sym}" if sym else "Bithumb 充提暂停"
            if "점검" in title:
                return "Bithumb 系统维护"
        return f"{exchange} 维护/暂停：{sym}" if sym else f"{exchange} 维护/暂停公告"
    if exchange == "Bithumb":
        return f"Bithumb 公告：{sym}" if sym else "Bithumb 公告"
    return title


def build_event(
    exchange: str,
    source: str,
    native_id: str,
    title: str,
    url: str,
    published: datetime | None,
    catalog: str = "",
    event_type: str | None = None,
) -> dict | None:
    title = (title or "").strip()
    if not title or not published:
        return None
    etype = event_type or classify(title)
    symbols = extract_symbols(title)
    return {
        "id": f"{source}:{native_id}",
        "exchange": exchange,
        "source": source,
        "native_id": str(native_id),
        "title": title,
        "display_title": display_title(exchange, etype, symbols, title),
        "url": url,
        "published_at": iso(published),
        "first_seen_at": iso(now_utc()),
        "event_type": etype,
        "type_label": TYPE_LABELS.get(etype, "其他"),
        "symbols": symbols,
        "importance": importance(exchange, etype),
        "summary": make_summary(exchange, etype, symbols),
        "catalog": catalog,
    }


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/xml, application/xml, */*",
    }
    headers.update(kwargs.pop("headers", {}))
    return session.get(url, headers=headers, timeout=TIMEOUT, **kwargs)


def result(name: str, events: list[dict], elapsed_ms: int, error: str | None = None) -> dict:
    return {"name": name, "ok": error is None, "count": len(events), "error": error, "elapsed_ms": elapsed_ms}


# ---------------- Binance CMS ----------------
BINANCE_CMS = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    "?type=1&catalogId={catalog}&pageNo={page}&pageSize=50"
)
BINANCE_CATALOGS = {48: "新币上市", 161: "下架", 49: "币安新闻"}


def fetch_binance(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    errors: list[str] = []
    try:
        for catalog, catalog_name in BINANCE_CATALOGS.items():
            pages = 6 if catalog in (48, 161) else 8
            for page in range(1, pages + 1):
                r = get(session, BINANCE_CMS.format(catalog=catalog, page=page))
                if r.status_code != 200:
                    errors.append(f"catalog {catalog} p{page}: HTTP {r.status_code}")
                    continue
                arts = r.json().get("data", {}).get("catalogs", [{}])[0].get("articles", [])
                if not arts:
                    break
                for a in arts:
                    etype = classify(str(a.get("title") or ""))
                    if catalog == 49 and etype in ("other", "maintenance"):
                        continue
                    ev = build_event(
                        "Binance", "binance", str(a.get("id") or a.get("code") or ""),
                        str(a.get("title") or ""),
                        f"https://www.binance.com/en/support/announcement/{a.get('code') or a.get('id')}",
                        parse_ms(a.get("releaseDate")), catalog_name,
                    )
                    if ev:
                        events.append(ev)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return events, result("binance", events, int((time.perf_counter() - t0) * 1000), "; ".join(errors) or None)


# ---------------- Bithumb ----------------
def fetch_bithumb(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = get(session, "https://api.bithumb.com/v1/notices?count=20")
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            items = r.json()
            if not isinstance(items, list):
                err = "unexpected response shape"
            else:
                for item in items:
                    url = str(item.get("pc_url") or "")
                    cats = ", ".join(item.get("categories") or [])
                    etype = None
                    if "마켓 추가" in cats:
                        etype = "listing"
                    elif "거래지원 종료" in cats or "상장폐지" in cats:
                        etype = "delisting"
                    elif "투자 유의" in cats:
                        etype = "caution"
                    elif "입출금" in cats or "점검" in cats:
                        etype = "maintenance"
                    ev = build_event(
                        "Bithumb", "bithumb", url.rstrip("/").split("/")[-1] or str(item.get("title")),
                        str(item.get("title") or ""), url,
                        parse_bithumb_time(item.get("published_at")), cats, etype,
                    )
                    if ev:
                        events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("bithumb", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- Bybit ----------------
def fetch_bybit(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = get(session, "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=50&page=1")
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            for it in r.json().get("result", {}).get("list", []):
                ev = build_event(
                    "Bybit", "bybit", str(it.get("url") or it.get("title")),
                    str(it.get("title") or ""), str(it.get("url") or ""),
                    parse_ms(it.get("publishTime") or it.get("dateTimestamp")),
                    str((it.get("type") or {}).get("title") or ""),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("bybit", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- OKX ----------------
def fetch_okx(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        for page in range(1, 4):
            r = get(session, f"https://www.okx.com/api/v5/support/announcements?page={page}")
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            groups = r.json().get("data", [])
            if not groups:
                break
            for group in groups:
                for it in group.get("details", []):
                    ev = build_event(
                        "OKX", "okx", str(it.get("url") or it.get("title")),
                        str(it.get("title") or ""), str(it.get("url") or ""),
                        parse_ms(it.get("pTime")), str(it.get("annType") or ""),
                    )
                    if ev and ev["event_type"] != "other":
                        events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("okx", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- Bitget ----------------
def fetch_bitget(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        for page in range(1, 3):
            r = get(session, f"https://api.bitget.com/api/v2/public/annoucements?language=en_US&pageSize=50&pageNo={page}")
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            items = r.json().get("data", [])
            if not items:
                break
            for it in items:
                ev = build_event(
                    "Bitget", "bitget", str(it.get("annId") or it.get("annTitle")),
                    str(it.get("annTitle") or ""), str(it.get("annUrl") or ""),
                    parse_ms(it.get("cTime")), str(it.get("annType") or ""),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("bitget", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- KuCoin ----------------
def fetch_kucoin(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        for page in range(1, 3):
            r = get(session, f"https://api.kucoin.com/api/v3/announcements?lang=en_US&pageSize=50&currentPage={page}")
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            items = r.json().get("data", {}).get("items", [])
            if not items:
                break
            for it in items:
                ev = build_event(
                    "KuCoin", "kucoin", str(it.get("annId") or it.get("annTitle")),
                    str(it.get("annTitle") or ""), str(it.get("annUrl") or ""),
                    parse_ms(it.get("cTime")), ", ".join(it.get("annType") or []),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("kucoin", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- Gate ----------------
def fetch_gate(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        for page in range(1, 4):
            r = session.post(
                "https://api.gateio.ws/api/v4/ann/list_article",
                json={"page": page, "page_size": 50},
                headers={"User-Agent": UA, "Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            items = r.json().get("data", {}).get("list", [])
            if not items:
                break
            for it in items:
                ev = build_event(
                    "Gate", "gate", str(it.get("id") or it.get("title")),
                    str(it.get("title") or ""),
                    "https://www.gate.io" + str(it.get("url") or ""),
                    parse_s(it.get("release_timestamp") or it.get("created_t")),
                    str(it.get("tags") or ""),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("gate", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- MEXC ----------------
def fetch_mexc(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = get(session, "https://www.mexc.com/api/v3/announcements?page=1&pageSize=50",
                headers={"Referer": "https://www.mexc.com/"})
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            data = r.json().get("data", r.json())
            items = data.get("list", data.get("items", [])) if isinstance(data, dict) else data
            for it in items:
                ev = build_event(
                    "MEXC", "mexc", str(it.get("id") or it.get("url") or it.get("title")),
                    str(it.get("title") or ""), str(it.get("url") or ""),
                    parse_ms(it.get("postTime") or it.get("cTime")), str(it.get("type") or ""),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("mexc", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- Crypto.com ----------------
def fetch_crypto_com(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = session.post(
            "https://api.crypto.com/exchange/v1/public/get-announcements",
            json={"page": 1, "page_size": 50},
            headers={"User-Agent": UA, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            data = r.json().get("result", {}).get("data", [])
            for it in data:
                ev = build_event(
                    "Crypto.com", "crypto_com", str(it.get("id") or it.get("title")),
                    str(it.get("title") or ""), str(it.get("url") or ""),
                    parse_ms(it.get("publish_time") or it.get("created_at")),
                    str(it.get("category") or ""),
                )
                if ev and ev["event_type"] != "other":
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("crypto_com", events, int((time.perf_counter() - t0) * 1000), err)


# ---------------- Coinbase status RSS ----------------
def fetch_coinbase_status(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = get(session, "https://status.coinbase.com/history.rss")
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = item.findtext("pubDate")
                published = parsedate_to_datetime(pub) if pub else None
                text = f"{title} {desc}"
                etype = "maintenance" if MAINT_RE.search(text) else "other"
                if etype == "other":
                    continue
                ev = build_event(
                    "Coinbase", "coinbase_status", link or title, title, link,
                    published, "Status Incident", etype,
                )
                if ev:
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("coinbase_status", events, int((time.perf_counter() - t0) * 1000), err)


def fetch_kraken_status(session: requests.Session) -> tuple[list[dict], dict]:
    t0 = time.perf_counter()
    events: list[dict] = []
    err = None
    try:
        r = get(session, "https://status.kraken.com/history.rss")
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = item.findtext("pubDate")
                published = parsedate_to_datetime(pub) if pub else None
                text = f"{title} {desc}"
                etype = "maintenance" if MAINT_RE.search(text) else "other"
                if etype == "other":
                    continue
                ev = build_event(
                    "Kraken", "kraken_status", link or title, title, link,
                    published, "Status Incident", etype,
                )
                if ev:
                    events.append(ev)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return events, result("kraken_status", events, int((time.perf_counter() - t0) * 1000), err)


SOURCES = [
    ("binance", fetch_binance),
    ("bithumb", fetch_bithumb),
    ("bybit", fetch_bybit),
    ("okx", fetch_okx),
    ("bitget", fetch_bitget),
    ("kucoin", fetch_kucoin),
    ("gate", fetch_gate),
    ("mexc", fetch_mexc),
    ("crypto_com", fetch_crypto_com),
    ("coinbase_status", fetch_coinbase_status),
    ("kraken_status", fetch_kraken_status),
]


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {"meta": {}, "events": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"meta": {}, "events": []}


def merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_id = {e["id"]: e for e in existing if e.get("id")}
    for old in by_id.values():
        if not old.get("display_title"):
            old["display_title"] = display_title(
                old.get("exchange", ""), old.get("event_type", "other"),
                old.get("symbols", []), old.get("title", ""),
            )
    for e in incoming:
        old = by_id.get(e["id"])
        if old:
            e["first_seen_at"] = old.get("first_seen_at") or e["first_seen_at"]
        by_id[e["id"]] = e
    events = sorted(by_id.values(), key=lambda x: x.get("published_at", ""), reverse=True)
    return events[:MAX_EVENTS]


def main() -> int:
    global MAX_EVENTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--max-events", type=int, default=MAX_EVENTS)
    args = ap.parse_args()
    MAX_EVENTS = args.max_events
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    all_events: list[dict] = []
    statuses: dict[str, Any] = {}
    started = time.perf_counter()
    for name, fn in SOURCES:
        events, status = fn(session)
        all_events.extend(events)
        statuses[name] = status
        print(f"{name}: {status['count']} events, {status['elapsed_ms']} ms"
              + (f", error={status['error']}" if status.get("error") else ""), flush=True)

    existing = load_existing(out_path)
    events = merge(existing.get("events", []), all_events)
    payload = {
        "meta": {
            "generated_at": iso(now_utc()),
            "project": "Exchange Risk Radar",
            "version": "0.2",
            "sources": statuses,
            "count": len(events),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
        "events": events,
    }
    # Compact JSON to keep the static file small and memory-light.
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"out": str(out_path), "events": len(events)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
