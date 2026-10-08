"""Métadonnées des tokens (fichier JSON pointé par l'URI de création, souvent sur IPFS) :
réseaux sociaux, site, description. Les vrais projets ont souvent une présence sociale,
les tokens jetables rarement. Récupération asynchrone, sans bloquer le flux."""
from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import httpx

# Mesuré depuis le VPS : ipfs.io renvoie 429 (trop de requêtes) ; la passerelle de pump.fun répond
# en ~0,16 s ; gateway.pinata.cloud fonctionne mais lentement (secours).
IPFS_GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://gateway.pinata.cloud/ipfs/")
_CID = re.compile(r"/ipfs/([A-Za-z0-9]+)")


def candidate_urls(uri: str) -> list[str]:
    if not uri:
        return []
    m = _CID.search(uri)
    if not m:
        return [uri]
    gws = [g + m.group(1) for g in IPFS_GATEWAYS]
    if "ipfs.io/" in uri:
        return gws                         # ipfs.io nous limite : on ne l'utilise pas
    return [uri] + [g for g in gws if g != uri]


def _clean(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def meta_features(meta: dict | None) -> dict[str, float]:
    """Features numériques (pures, testées). `meta_fetched` = 0 si le fichier n'a pas pu être lu."""
    if not meta:
        return {"meta_fetched": 0.0}
    tw, tg, web = _clean(meta.get("twitter")), _clean(meta.get("telegram")), _clean(meta.get("website"))
    desc = _clean(meta.get("description"))
    f = {
        "meta_fetched": 1.0,
        "meta_has_twitter": float(bool(tw)),
        "meta_has_telegram": float(bool(tg)),
        "meta_has_website": float(bool(web)),
        "meta_n_socials": float(bool(tw) + bool(tg) + bool(web)),
        "meta_desc_len": math.log1p(len(desc)),
        # un lien vers un tweet (et non un compte) = souvent un token « narratif » opportuniste
        "meta_twitter_is_post": float("/status/" in tw),
        "meta_twitter_is_community": float("/communities/" in tw),
        "meta_website_is_social": float(any(s in web for s in ("x.com", "twitter.com", "t.me"))),
    }
    return f


class MetadataFetcher:
    def __init__(self, timeout_s: float = 4.0, concurrency: int = 20):
        self.client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True,
                                        headers={"User-Agent": "apex-scanner/1.0"})
        self.sem = asyncio.Semaphore(concurrency)
        self.stats = {"ok": 0, "fail": 0}

    async def fetch(self, uri: str) -> dict | None:
        async with self.sem:
            for url in candidate_urls(uri):
                try:
                    r = await self.client.get(url)
                    if r.status_code == 200:
                        d = r.json()
                        if isinstance(d, dict):
                            self.stats["ok"] += 1
                            return {k: d.get(k) for k in ("twitter", "telegram", "website", "description", "image",
                                                          "showName", "createdOn")}
                except (httpx.HTTPError, ValueError):
                    continue
            self.stats["fail"] += 1
            return None
