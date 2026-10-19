"""Scarica e conserva i dati di F1 Live.

Tutto sta nella cartella dei dati (fuori dalla Scrivania, che è sincronizzata con iCloud):
- cache/stagione-<anno>.json: calendario e risultati Jolpica, riunioni e sessioni OpenF1;
- cache/sessioni/<chiave>.json: ogni sessione in forma compatta (giri, gomme, pit stop, direzione gara, meteo,
  sorpassi, risultato). Una sessione finita e completa non si riscarica più;
- cache/circuiti.json: la storia di ogni circuito dal 2023 (safety car, bandiere rosse, soste, pioggia, quanto
  conta la griglia), da OpenF1 e Jolpica;
- cache/tracciati/<circuito>.json: il disegno del tracciato e il tempo perso ai box (MultiViewer);
- cache/meteo.json: le previsioni per il prossimo weekend (Open-Meteo).
"""
import json
import os
import time
from pathlib import Path

from fonti import NonDisponibile, iso, ts

DATA = Path(os.environ.get("F1LIVE_DATA_DIR") or Path.home() / "Library" / "Application Support" / "F1 Live")
CACHE = DATA / "cache"
ANNI_STORIA = (2023, 2024, 2025)
GARA = ("Race", "Sprint")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def leggi(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def scrivi(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)


def file_sessione(sk):
    return CACHE / "sessioni" / f"{sk}.json"


# ---------------------------------------------------------------- stagione

async def stagione(f, anno, vecchia=None):
    """Calendario e risultati Jolpica, riunioni e sessioni OpenF1 di un anno. Se OpenF1 è chiuso (durante una
    sessione, senza abbonamento) riunioni e sessioni restano quelle già salvate."""
    d = {"anno": anno, "at": time.time()}
    d["gare"] = await f.jolpica(f"{anno}/races", "RaceTable", "Races")
    d["risultati"] = await f.jolpica_righe(f"{anno}/results", "RaceTable", "Races", "Results")
    d["qualifiche"] = await f.jolpica_righe(f"{anno}/qualifying", "RaceTable", "Races", "QualifyingResults")
    d["sprint"] = await f.jolpica_righe(f"{anno}/sprint", "RaceTable", "Races", "SprintResults")
    pil = await f.jolpica(f"{anno}/driverstandings", "StandingsTable", "StandingsLists")
    cos = await f.jolpica(f"{anno}/constructorstandings", "StandingsTable", "StandingsLists")
    d["classifica_piloti"] = pil[-1] if pil else None
    d["classifica_costruttori"] = cos[-1] if cos else None
    try:
        d["riunioni"] = await f.openf1("meetings", year=anno)
        d["sessioni"] = await f.openf1("sessions", year=anno)
    except NonDisponibile:
        if not vecchia:
            raise
        d["riunioni"], d["sessioni"] = vecchia.get("riunioni") or [], vecchia.get("sessioni") or []
    return d


# ---------------------------------------------------------------- sessioni

def _neutralizzati(rc):
    """Giri sotto safety car, virtual safety car o bandiera rossa, dai messaggi della direzione gara."""
    giri, inizio = set(), None
    for e in rc:
        lap, msg = e[1], (e[4] or "").upper()
        if lap is None:
            continue
        if e[2] == "SafetyCar":
            if "DEPLOYED" in msg:
                inizio = lap if inizio is None else inizio
            elif ("IN THIS LAP" in msg or "ENDING" in msg) and inizio is not None:
                giri.update(range(inizio, lap + 2))
                inizio = None
        if e[3] == "RED":
            giri.update((lap, lap + 1))
    if inizio is not None:
        giri.update(range(inizio, inizio + 4))
    return sorted(giri)


def posizione(r):
    """OpenF1 a volte mette una sigla al posto della posizione («RT» ritirato, «DQ», «NC», «DNS»): la posizione
    diventa vuota e la sigla finisce nei campi dnf, dsq e dns."""
    p, sigla = r.get("pos"), None
    if isinstance(p, str):
        p, sigla = (int(p), None) if p.isdigit() else (None, p.upper())
    if sigla:
        r["dnf"] = r.get("dnf") or sigla in ("RT", "RET", "NC", "DNF")
        r["dsq"] = r.get("dsq") or sigla in ("DQ", "DSQ")
        r["dns"] = r.get("dns") or sigla in ("DNS",)
    r["pos"] = p if isinstance(p, int) else None
    return r


def compatta(s, piloti, giri, gomme, rc, meteo, risultato, pit, sorpassi):
    rc_k = []
    for e in rc:
        msg = e.get("message") or ""
        if (e.get("category") in ("SafetyCar", "SessionStatus") or e.get("flag") in ("RED", "CHEQUERED")
                or ("PENALTY" in msg and "SERVED" not in msg) or "DISQUALIFIED" in msg or "DELETED" in msg
                or "INVESTIGATION" in msg):
            rc_k.append([ts(e.get("date")), e.get("lap_number"), e.get("category"), e.get("flag"), msg[:160],
                         e.get("driver_number")])
    pioggia = any((w.get("rainfall") or 0) > 0 for w in meteo)
    # ogni tanto il sensore manda uno zero: si scarta
    aria = [w["air_temperature"] for w in meteo if (w.get("air_temperature") or 0) > 1]
    asfalto = [w["track_temperature"] for w in meteo if (w.get("track_temperature") or 0) > 1]
    return {
        "sk": s["session_key"], "mk": s["meeting_key"], "nome": s["session_name"], "tipo": s["session_type"],
        "inizio": ts(s["date_start"]), "fine": ts(s["date_end"]), "circuito": s.get("circuit_key"),
        "piloti": {str(p["driver_number"]): {"sigla": p.get("name_acronym"), "nome": p.get("full_name"),
                                             "nome_breve": p.get("broadcast_name"), "squadra": p.get("team_name"),
                                             "colore": p.get("team_colour"), "foto": p.get("headshot_url")}
                   for p in piloti},
        # pilota, giro, tempo, settore 1, 2, 3, giro di uscita dai box, velocità al rilevamento, inizio del giro
        "giri": [[g["driver_number"], g["lap_number"], g.get("lap_duration"), g.get("duration_sector_1"),
                  g.get("duration_sector_2"), g.get("duration_sector_3"), 1 if g.get("is_pit_out_lap") else 0,
                  g.get("st_speed"), round(ts(g["date_start"]), 1) if g.get("date_start") else None] for g in giri],
        "gomme": [[g["driver_number"], g["stint_number"], g.get("lap_start"), g.get("lap_end"), g.get("compound"),
                   g.get("tyre_age_at_start")] for g in gomme],
        "pit": [[p["driver_number"], p.get("lap_number"), p.get("lane_duration") or p.get("pit_duration"),
                 p.get("stop_duration")] for p in pit],
        "rc": rc_k,
        "neutralizzati": _neutralizzati(rc_k),
        "meteo": {"pioggia": pioggia, "aria": [min(aria), max(aria)] if aria else None,
                  "asfalto": [min(asfalto), max(asfalto)] if asfalto else None},
        "sorpassi": [[o["overtaking_driver_number"], o["overtaken_driver_number"], o.get("position")] for o in sorpassi],
        "risultato": [posizione({"pos": r.get("position"), "n": r["driver_number"], "giri": r.get("number_of_laps"),
                                 "dnf": r.get("dnf"), "dns": r.get("dns"), "dsq": r.get("dsq"), "tempo": r.get("duration"),
                                 "distacco": r.get("gap_to_leader"), "punti": r.get("points")}) for r in risultato],
    }


async def scarica_sessione(f, s, completa=True):
    sk = s["session_key"]
    gara = s["session_name"] in GARA
    piloti = await f.openf1("drivers", session_key=sk)
    risultato = await f.openf1("session_result", session_key=sk)
    rc = await f.openf1("race_control", session_key=sk)
    meteo = await f.openf1("weather", session_key=sk)
    gomme = await f.openf1("stints", session_key=sk)
    giri = await f.openf1("laps", session_key=sk) if completa else []
    pit = await f.openf1("pit", session_key=sk) if gara else []
    sorpassi = await f.openf1("overtakes", session_key=sk) if gara and completa else []
    return compatta(s, piloti, giri, gomme, rc, meteo, risultato, pit, sorpassi)


async def aggiorna_sessioni(f, sessioni, adesso, limite=None):
    """Scarica le sessioni finite che mancano o sono ancora incomplete. Restituisce quante ne ha aggiornate."""
    fatte = 0
    for s in sorted(sessioni, key=lambda s: s["date_start"]):
        if s.get("is_cancelled") or s["session_type"] == "Practice" and s["session_name"].startswith("Day"):
            continue   # test invernali
        fine = ts(s["date_end"])
        if fine is None or adesso < fine + 10 * 60:
            continue
        path = file_sessione(s["session_key"])
        vecchio = leggi(path)
        if vecchio and vecchio.get("definitiva"):
            continue
        if vecchio and adesso - vecchio.get("at", 0) < 20 * 60:
            continue
        try:
            d = await scarica_sessione(f, s)
        except NonDisponibile:   # OpenF1 chiuso durante una sessione: si riprova al giro dopo
            break
        except RuntimeError as e:   # la fonte non risponde: si riprova al giro dopo
            log(f"Sessione {s['session_key']} non scaricata: {e}")
            break
        d["at"] = adesso
        # OpenF1 completa i dati entro poche ore: dopo 6 ore con risultato e giri la sessione non si riscarica più
        d["definitiva"] = bool(d["risultato"] and d["giri"] and adesso > fine + 6 * 3600)
        scrivi(path, d)
        log(f"Sessione {s['session_key']} {s['circuit_short_name']} {s['session_name']}: {len(d['giri'])} giri")
        fatte += 1
        if limite and fatte >= limite:
            break
    return fatte


def sessioni_salvate(chiavi):
    out = {}
    for sk in chiavi:
        d = leggi(file_sessione(sk))
        if d:
            for r in d.get("risultato") or []:   # anche le sessioni salvate prima della correzione
                posizione(r)
            out[sk] = d
    return out


# ---------------------------------------------------------------- storia dei circuiti

async def storia_circuiti(f, vecchia):
    """Per ogni gara e sprint dal 2023: safety car, virtual, bandiere rosse, soste dei piloti arrivati, mescole,
    pioggia, e da Jolpica griglia e arrivo (per capire quanto è difficile sorpassare)."""
    vecchia = vecchia or {}
    gare = {g["sk"]: g for g in vecchia.get("gare", [])}
    for anno in ANNI_STORIA:
        if str(anno) in vecchia.get("anni", []):
            continue
        sess = [s for s in await f.openf1("sessions", year=anno) if s["session_name"] in GARA]
        jr = await f.jolpica_righe(f"{anno}/results", "RaceTable", "Races", "Results")
        js = await f.jolpica_righe(f"{anno}/sprint", "RaceTable", "Races", "SprintResults")
        # le sprint si riconoscono dalla data della gara dello stesso weekend
        per_data = {(g["date"], "Race"): g["Results"] for g in jr}
        per_data.update({(g["date"], "Sprint"): g["SprintResults"] for g in js})
        data_gara = {s["meeting_key"]: s["date_start"][:10] for s in sess if s["session_name"] == "Race"}
        for s in sess:
            if s["session_key"] in gare or s.get("is_cancelled"):
                continue
            sk = s["session_key"]
            rc = await f.openf1("race_control", session_key=sk)
            gomme = await f.openf1("stints", session_key=sk)
            meteo = await f.openf1("weather", session_key=sk)
            jres = per_data.get((data_gara.get(s["meeting_key"], s["date_start"][:10]), s["session_name"])) or []
            gare[sk] = riassunto_gara(s, rc, gomme, meteo, jres)
            log(f"Storia {anno} {s['circuit_short_name']} {s['session_name']}")
        vecchia.setdefault("anni", []).append(str(anno))
        vecchia["gare"] = list(gare.values())
    return vecchia


def riassunto_gara(s, rc, gomme, meteo, jres):
    msgs = [(e.get("category"), e.get("flag"), (e.get("message") or "").upper()) for e in rc]
    sc = sum(1 for c, _, m in msgs if c == "SafetyCar" and m.startswith("SAFETY CAR DEPLOYED"))
    vsc = sum(1 for c, _, m in msgs if c == "SafetyCar" and ("VIRTUAL SAFETY CAR DEPLOYED" in m or m.startswith("VSC DEPLOYED")))
    rosse = sum(1 for _, fl, _ in msgs if fl == "RED")
    arrivati = {int(r["number"]) for r in jres if r.get("status") in ("Finished",) or str(r.get("status", "")).startswith("+")
                or (r.get("status") == "Lapped")}
    soste, sequenze = [], []
    per_pilota = {}
    for g in gomme:
        per_pilota.setdefault(g["driver_number"], []).append(g)
    for n, st in per_pilota.items():
        if n not in arrivati:
            continue
        st.sort(key=lambda g: g["stint_number"])
        soste.append(len(st) - 1)
        sequenze.append("-".join((g.get("compound") or "?")[0] for g in st))
    coppie = [(int(r["grid"]), int(r["position"])) for r in jres
              if r.get("grid") not in (None, "0") and int(r["number"]) in arrivati]
    return {"sk": s["session_key"], "anno": s["year"], "circuito": s["circuit_key"], "nome": s["circuit_short_name"],
            "tipo": s["session_name"], "sc": sc, "vsc": vsc, "rosse": rosse,
            "giri": max([g.get("lap_end") or 0 for g in gomme] or [0]) or None,
            "pioggia": any((w.get("rainfall") or 0) > 0 for w in meteo),
            "soste": soste, "sequenze": sequenze, "griglia_arrivo": coppie}


# ---------------------------------------------------------------- tracciati e meteo

async def tracciato(f, circuit_key, anno):
    path = CACHE / "tracciati" / f"{circuit_key}.json"
    vecchio = leggi(path)
    if vecchio and vecchio.get("anno") == anno:
        return vecchio
    try:
        d = await f.tracciato(circuit_key, anno)
    except Exception:
        d = None
    if not d or not d.get("x"):
        return vecchio
    passo = max(1, len(d["x"]) // 260)   # basta un punto ogni tanto per disegnarlo
    pit = d.get("pitLoss") or {}
    if not isinstance(pit, dict):
        pit = {"normal": pit}
    num = lambda v: float(v) if v not in (None, "") else None
    out = {"anno": anno, "x": d["x"][::passo], "y": d["y"][::passo], "rotazione": d.get("rotation") or 0,
           # secondi persi con un pit stop: normale, sotto safety car e sotto virtual safety car
           "pit": num(pit.get("normal")), "pit_sc": num(pit.get("sc")), "pit_vsc": num(pit.get("vsc")), "curve": [[c["number"], round(c["trackPosition"]["x"]), round(c["trackPosition"]["y"])]
                                             for c in d.get("corners") or []]}
    scrivi(path, out)
    return out


async def tracciato_da_openf1(f, ck, sess, anno):
    """Per i circuiti che MultiViewer non ha (Madrid, Sepang): la forma della pista ricavata dalle posizioni in pista
    di un giro pulito del vincitore (OpenF1, endpoint location). Senza curve numerate né tempo dei box."""
    path = CACHE / "tracciati" / f"{ck}.json"
    vinc = next((r["n"] for r in sess.get("risultato") or [] if r.get("pos") == 1), None)
    giri = sorted([g for g in sess.get("giri") or [] if g[0] == vinc and g[2] and not g[6] and g[8] and g[1] > 1],
                  key=lambda g: g[2])
    if not giri:
        return None
    g = giri[0]
    punti = await f.openf1("location", session_key=sess["sk"], driver_number=vinc,
                           date__gte=iso(g[8]), date__lt=iso(g[8] + g[2]))
    punti = [p for p in punti if p.get("x") is not None and (p.get("x") or p.get("y"))]
    if len(punti) < 50:
        return None
    passo = max(1, len(punti) // 260)
    out = {"anno": anno, "x": [p["x"] for p in punti][::passo], "y": [p["y"] for p in punti][::passo], "rotazione": 0,
           "pit": None, "curve": [], "fonte": "OpenF1"}
    scrivi(path, out)
    return out


async def meteo_weekend(f, gara):
    """Previsioni orarie per i giorni del weekend: servono solo se il weekend è entro una settimana."""
    loc = gara["Circuit"]["Location"]
    giorni = sorted({gara[k]["date"] for k in ("FirstPractice", "SecondPractice", "ThirdPractice", "Qualifying",
                                                   "Sprint", "SprintQualifying") if k in gara} | {gara["date"]})
    d = await f.meteo(loc["lat"], loc["long"], giorni[0], giorni[-1])
    h = (d or {}).get("hourly") or {}
    return {"round": gara["round"], "at": time.time(), "ore": [
        [ts(t + ":00Z"), p, mm, temp, v] for t, p, mm, temp, v in zip(
            h.get("time", []), h.get("precipitation_probability", []), h.get("precipitation", []),
            h.get("temperature_2m", []), h.get("wind_speed_10m", []))]}
