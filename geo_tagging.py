"""
Geographic tagging via labelled exchanges and OFAC country provenance.

On-chain data has NO country / IP columns. This module derives a geographic
overlay from the two labelled address sources we do have:

  1. Known exchange addresses -> the exchanges' known operating countries.
  2. OFAC-sanctioned addresses -> the OFAC listing's target country.

If a candidate touches an exchange with a country tag, or is within N hops of
an OFAC address with a country tag, we tag the candidate with that country.
This is coarse but honest: it says "this funnel routes through Iran-facing
infrastructure", not "this operator sits in Iran".

The country lists are FREE and static (baked in below). To extend, edit
`EXCHANGE_COUNTRIES` and `OFAC_COUNTRIES` in place. Only countries the user
picked (INTERESTING_COUNTRIES) are surfaced by default; the rest are dropped
to keep the output focused.
"""
from __future__ import annotations
from typing import Iterable, Dict, List, Set
import pandas as pd


INTERESTING_COUNTRIES: Set[str] = {"IL", "IR", "LB", "SY", "AE", "RU", "KP", "YE"}


EXCHANGE_COUNTRIES: Dict[str, List[str]] = {
    "binance": ["MT", "AE", "GLOBAL"],
    "coinbase": ["US"],
    "kraken": ["US"],
    "gemini": ["US"],
    "bitstamp": ["LU", "US"],
    "kucoin": ["SC", "GLOBAL"],
    "okx": ["SC", "AE"],
    "huobi": ["SC", "AE"],
    "bybit": ["AE", "GLOBAL"],
    "gate.io": ["KY", "GLOBAL"],
    "mexc": ["SC", "GLOBAL"],
    "bitfinex": ["VG"],
    "crypto-com": ["SG", "MT"],
    "bitget": ["SC"],
    "garantex": ["RU"],
    "wex": ["RU"],
    "btc-e": ["RU"],
    "bit2c": ["IL"],
    "bitsofgold": ["IL"],
    "bitcoin-of-israel": ["IL"],
    "yellowcard": ["NG"],
    "bitso": ["MX"],
    "buda": ["CL"],
    "luno": ["ZA", "MY"],
    "nobitex": ["IR"],
    "wallex": ["IR"],
    "excoino": ["IR"],
    "rain": ["BH", "AE"],
    "bitoasis": ["AE"],
    "cryptolocal": ["AE"],
    "bitpanda": ["AT"],
    "n26": ["DE"],
    "bitkub": ["TH"],
}


OFAC_COUNTRIES: Dict[str, str] = {
    "iran": "IR",
    "north korea": "KP",
    "dprk": "KP",
    "russia": "RU",
    "russian": "RU",
    "syria": "SY",
    "cuba": "CU",
    "venezuela": "VE",
    "yemen": "YE",
    "lebanon": "LB",
    "hamas": "PS",
    "hezbollah": "LB",
    "houthi": "YE",
}


def exchange_countries(name: str) -> List[str]:
    """Return the country list associated with an exchange name (case-insensitive
    fuzzy match against EXCHANGE_COUNTRIES keys)."""
    if not name:
        return []
    lo = str(name).lower()
    hits = []
    for key, countries in EXCHANGE_COUNTRIES.items():
        if key in lo:
            hits.extend(countries)
    return list(dict.fromkeys(hits))


def ofac_country_from_listing(listing_text: str) -> str:
    """Extract a country code from a free-text OFAC listing / entity name."""
    if not listing_text:
        return ""
    lo = str(listing_text).lower()
    for token, code in OFAC_COUNTRIES.items():
        if token in lo:
            return code
    return ""


def annotate_geo(
    master: pd.DataFrame,
    anchors: pd.DataFrame | None = None,
    nbctf_meta: dict | None = None,
    interesting_countries: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Attach geo columns to `master`:
      - country_codes  : list[str] of country codes touched by this candidate
      - country_source : "exchange" | "nbctf" | "exchange+nbctf" | ""
      - hits_interesting_country : bool - candidate touched a country in the
        user's interesting-country set

    Sources:
      * anchors DataFrame (candidate->exchange->exchange_name) contributes
        exchange countries.
      * nbctf_meta {addr_lower: {"affiliation": ...}} contributes NBCTF country.
    """
    interesting = set(interesting_countries or INTERESTING_COUNTRIES)
    country_map: Dict[str, Set[str]] = {}
    src_map: Dict[str, Set[str]] = {}

    if anchors is not None and len(anchors):
        for r in anchors.itertuples(index=False):
            w = getattr(r, "candidate", None) or getattr(r, "wallet", None)
            if not w:
                continue
            name = getattr(r, "exchange_name", "") or ""
            countries = exchange_countries(name)
            if countries:
                country_map.setdefault(w, set()).update(countries)
                src_map.setdefault(w, set()).add("exchange")

    if nbctf_meta:
        for addr, meta in nbctf_meta.items():
            code = ofac_country_from_listing(meta.get("affiliation", ""))
            if code:
                country_map.setdefault(addr, set()).add(code)
                src_map.setdefault(addr, set()).add("nbctf")

    out = master.copy()
    w = out["wallet"].astype(str).str.lower()

    def _lookup(x):
        return sorted(country_map.get(x) or country_map.get(x.lower()) or set())

    def _sources(x):
        s = src_map.get(x) or src_map.get(x.lower()) or set()
        return "+".join(sorted(s))

    out["country_codes"] = out["wallet"].map(_lookup)
    out["country_source"] = out["wallet"].map(_sources)
    out["hits_interesting_country"] = out["country_codes"].map(
        lambda cs: bool(set(cs) & interesting))
    return out


def country_summary(annotated: pd.DataFrame,
                    interesting_countries: Iterable[str] | None = None) -> pd.DataFrame:
    """Roll up candidate counts by country. Only rows with a country get counted.
    Interesting-country flag is preserved for downstream filtering."""
    interesting = set(interesting_countries or INTERESTING_COUNTRIES)
    if not len(annotated) or "country_codes" not in annotated.columns:
        return pd.DataFrame(columns=["country", "n_candidates", "is_interesting"])
    rows = []
    for _, r in annotated.iterrows():
        for c in (r["country_codes"] or []):
            rows.append({"country": c, "chain": r.get("chain", "")})
    if not rows:
        return pd.DataFrame(columns=["country", "n_candidates", "is_interesting"])
    df = pd.DataFrame(rows)
    agg = (df.groupby("country").size().rename("n_candidates").reset_index()
           .sort_values("n_candidates", ascending=False))
    agg["is_interesting"] = agg["country"].isin(interesting)
    return agg
