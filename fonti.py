"""Le fonti di F1 Live, tutte gratuite e senza chiavi.

- Jolpica (l'erede di Ergast): calendario, risultati con griglia e motivo del ritiro, qualifiche, sprint, classifiche.
- OpenF1: per ogni sessione giri con i settori, gomme, pit stop, direzione gara (bandiere, safety car, penalità),
  meteo in pista, sorpassi e risultati. Limite: 3 richieste al secondo e 30 al minuto.
- Open-Meteo: previsioni del tempo sul circuito.
- MultiViewer: il disegno del tracciato e il tempo perso per un pit stop.
"""
import asyncio
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import aiohttp
from urllib.parse import quote
from yarl import URL

JOLPICA = "https://api.jolpi.ca/ergast/f1"
OPENF1 = "https://api.openf1.org/v1"
METEO = "https://api.open-meteo.com/v1/forecast"
TRACCIATI = "https://api.multiviewer.app/api/v1/circuits"
UA = {"User-Agent": "F1 Live (app personale)"}
SERVIZIO_PORTACHIAVI = "F1 Live OpenF1"


def credenziali_openf1():
    """Utente e password dell'abbonamento OpenF1 (serve solo per il live durante le sessioni). Sul Mac stanno nel
    Portachiavi (le salva «Collega abbonamento OpenF1.command»), altrove nelle variabili OPENF1_UTENTE e OPENF1_PASSWORD."""
    if os.environ.get("OPENF1_UTENTE") and os.environ.get("OPENF1_PASSWORD"):
        return os.environ["OPENF1_UTENTE"], os.environ["OPENF1_PASSWORD"]
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVIZIO_PORTACHIAVI, "-g"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    utente = re.search(r'"acct"<blob>="(.*)"', r.stdout)
    pw = re.search(r'^password: "(.*)"$', r.stderr, re.M)
    return (utente.group(1), pw.group(1)) if utente and pw else None


def ts(s):
    """Data ISO di OpenF1 o Jolpica in secondi (UTC)."""
    if not s:
        return None
    # Python 3.9 legge solo decimali di 3 o 6 cifre e non conosce la Z
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s.replace("Z", "+00:00"))
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Ritmo:
    """Distanzia le richieste a una fonte: OpenF1 ne accetta 3 al secondo, Jolpica 4."""

    def __init__(self, gap):
        self.gap, self.t, self.lock = gap, 0.0, asyncio.Lock()

    async def attendi(self):
        async with self.lock:
            w = self.t + self.gap - time.monotonic()
            if w > 0:
                await asyncio.sleep(w)
            self.t = time.monotonic()


class NonDisponibile(Exception):
    """La fonte risponde ma non dà quei dati (per esempio OpenF1 durante una sessione senza abbonamento)."""


class Fonti:
    def __init__(self):
        self.s = None
        self.r_of, self.r_jo, self.r_alt = Ritmo(2.05), Ritmo(0.3), Ritmo(0.2)   # OpenF1: 30 al minuto
        self.errori = {}   # fonte -> ultimo errore (si azzera quando torna a rispondere)
        self.pausa_openf1 = 0   # durante una sessione OpenF1 chiude tutto a chi non è abbonato: fino a quando non si riprova
        self.cred = credenziali_openf1()
        self.token, self.scade = None, 0
        if self.cred:   # con l'abbonamento OpenF1 concede più richieste
            self.r_of = Ritmo(0.4)

    @property
    def abbonato(self):
        return bool(self.cred)

    @property
    def openf1_in_pausa(self):
        return time.time() < self.pausa_openf1

    async def intestazioni_openf1(self):
        if not self.cred:
            return None
        if not self.token or time.time() > self.scade - 120:
            try:
                async with self.s.post(f"{OPENF1.rsplit('/', 1)[0]}/token",
                                       data={"username": self.cred[0], "password": self.cred[1]}) as r:
                    if r.status != 200:
                        self.errori["Abbonamento OpenF1"] = f"accesso non riuscito (HTTP {r.status})"
                        return None
                    d = await r.json(content_type=None)
                    self.token, self.scade = d["access_token"], time.time() + float(d.get("expires_in") or 3600)
                    self.errori.pop("Abbonamento OpenF1", None)
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
                return None
        return {"Authorization": f"Bearer {self.token}"}

    async def __aenter__(self):
        self.s = aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *a):
        await self.s.close()

    async def get(self, nome, url, params, ritmo, tentativi=4, headers=None):
        err = None
        for i in range(tentativi):
            await ritmo.attendi()
            try:
                async with self.s.get(url, params=params, headers=headers) as r:
                    if r.status == 429 or r.status >= 500:
                        err = f"HTTP {r.status}"
                        await asyncio.sleep((20 if r.status == 429 else 3) * (i + 1))
                        continue
                    if r.status in (401, 403):
                        raise NonDisponibile(f"HTTP {r.status}")
                    if r.status == 404:
                        self.errori.pop(nome, None)
                        return None
                    if r.status >= 400:
                        raise RuntimeError(f"HTTP {r.status}")
                    d = await r.json(content_type=None)
                    self.errori.pop(nome, None)
                    return d
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = type(e).__name__
                await asyncio.sleep(1 + 2 * i)
        self.errori[nome] = err or "nessuna risposta"
        raise RuntimeError(f"{nome}: {err}")

    # --- OpenF1 ---
    async def openf1(self, ep, **params):
        """params con operatori: date__gt='...' diventa date>... (lo stesso per lt, gte, lte)."""
        # l'indirizzo si scrive a mano: OpenF1 vuole gli operatori così come sono («date>=…»); codificati
        # («date%3E%3D=…») per il filtro sulle date risponde con un errore
        parti = []
        for k, v in params.items():
            op = "="
            for suf, o in (("__gte", ">="), ("__lte", "<="), ("__gt", ">"), ("__lt", "<")):
                if k.endswith(suf):
                    k, op = k[: -len(suf)], o
                    break
            parti.append(f"{k}{op}{quote(str(v), safe=':.-')}")
        url = URL(f"{OPENF1}/{ep}" + ("?" + "&".join(parti) if parti else ""), encoded=True)
        if time.time() < self.pausa_openf1:
            raise NonDisponibile("OpenF1 è chiuso durante la sessione")
        try:
            d = await self.get("OpenF1", url, None, self.r_of, headers=await self.intestazioni_openf1())
        except NonDisponibile:
            # «Live F1 session in progress»: niente dati, nemmeno quelli vecchi, finché la sessione non finisce
            self.pausa_openf1 = time.time() + 120
            raise
        if isinstance(d, dict):
            msg = str(d.get("detail") or d.get("error") or "")
            if "No results" in msg:
                return []
            if "subscri" in msg.lower() or "auth" in msg.lower() or "live" in msg.lower():
                raise NonDisponibile(msg)
            raise RuntimeError(f"OpenF1 {ep}: {msg}")
        return d or []

    # --- Jolpica ---
    async def jolpica(self, path, tabella, chiave):
        """Tutte le pagine di una richiesta Jolpica: restituisce la lista sotto MRData[tabella][chiave]."""
        out, offset = [], 0
        while True:
            d = await self.get("Jolpica", f"{JOLPICA}/{path}/", {"limit": 100, "offset": offset}, self.r_jo)
            if not d:
                return out
            m = d["MRData"]
            items = m[tabella][chiave]
            out.extend(items)
            offset += 100
            # i risultati sono raggruppati per gara: "total" conta le righe, non le gare
            if offset >= int(m["total"]):
                return out

    async def jolpica_righe(self, path, tabella, chiave, righe):
        """Come jolpica, ma per risultati e qualifiche: le gare spezzate tra due pagine vengono riunite."""
        gare = {}
        for g in await self.jolpica(path, tabella, chiave):
            r = gare.setdefault(g["round"], dict(g, **{righe: []}))
            r[righe].extend(g.get(righe) or [])
        return sorted(gare.values(), key=lambda g: int(g["round"]))

    # --- altre ---
    async def meteo(self, lat, lon, giorno_da, giorno_a):
        return await self.get("Open-Meteo", METEO, {
            "latitude": lat, "longitude": lon, "timezone": "UTC", "start_date": giorno_da, "end_date": giorno_a,
            "hourly": "precipitation_probability,precipitation,temperature_2m,wind_speed_10m"}, self.r_alt)

    async def tracciato(self, circuit_key, anno):
        return await self.get("MultiViewer", f"{TRACCIATI}/{circuit_key}/{anno}", None, self.r_alt)
