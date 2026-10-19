"""F1 Live: scarica i dati, calcola pronostici e statistiche e serve la pagina su http://127.0.0.1:8781.

    python server.py            pagina sul Mac, dati aggiornati da soli, live durante le sessioni
    python server.py --no-open  senza aprire il browser
    python server.py --offline  solo i dati già salvati, nessuna richiesta esterna
    python server.py --cloud    un solo aggiornamento, poi ripubblica il sito (lo usa GitHub Actions)
"""
import asyncio
import json
import math
import os
import random
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import aiohttp
from aiohttp import web

import analisi
import dati
import model
import sito
from dati import CACHE, DATA, leggi, log, scrivi
from fonti import Fonti, NonDisponibile, iso, ts

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("F1LIVE_PORT") or 8781)   # Serie A Live usa 8765 e seguenti, una per campionato
VERSIONE = "2026-10-19.2"
CLOUD = "--cloud" in sys.argv
ANNO = int(os.environ.get("F1LIVE_ANNO") or 2026)
SITE_DIR = Path(os.environ.get("F1LIVE_SITE_DIR") or DATA / "sito")
FILE_STAGIONE = CACHE / f"stagione-{ANNO}.json"
FILE_STORIA = CACHE / "circuiti.json"
FILE_METEO = CACHE / "meteo.json"
FILE_PARAMETRI = CACHE / "parametri.json"
FILE_STORICO = DATA / "storico.json"
FILE_FOTO = DATA / "fotografia.json"
FILE_ESECUZIONE = DATA / "esecuzione.json"
FILE_EXTRA = DATA / "extra.json"

STATO = {"foto": None, "live": None, "errori": {}, "calcolo": None}
EXTRA = leggi(FILE_EXTRA, {}) or {}


def salva_extra():
    scrivi(FILE_EXTRA, EXTRA)


def gh_note(msg, livello="error"):
    if CLOUD:
        msg = str(msg).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{livello}::{msg}", flush=True)


# ======================================================================= calcolo

def sessioni_della_stagione(st):
    return dati.sessioni_salvate([s["session_key"] for s in st.get("sessioni") or []])


def pioggia_eventi(meteo, w):
    """Probabilità di pioggia vera per sprint e gara: la previsione oraria più alta nelle due ore dell'evento,
    ridotta (una previsione di pioggia del 50% non vuol dire gara bagnata una volta su due)."""
    out = {}
    if not meteo or meteo.get("round") != str(w["round"]):
        return out
    for ev, nome in (("Race", "Gara"), ("Sprint", "Sprint")):
        t0 = ts(w["orari"].get(nome))
        if not t0:
            continue
        ps = [o[1] for o in meteo["ore"] if o[1] is not None and t0 - 1800 <= o[0] <= t0 + 2 * 3600]
        if ps:
            out[(w["round"], ev)] = max(ps) / 100 * 0.6
    return out


def meteo_sessioni(meteo, w):
    if not meteo or meteo.get("round") != str(w["round"]):
        return {}
    out = {}
    for nome, t in w["orari"].items():
        t0 = ts(t)
        ore = [o for o in meteo["ore"] if t0 - 1800 <= o[0] <= t0 + 2 * 3600]
        if ore:
            out[nome] = {"pioggia": max(o[1] or 0 for o in ore), "mm": round(sum(o[2] or 0 for o in ore), 1),
                         "temp": round(model.media([o[3] for o in ore]) or 0), "vento": round(max(o[4] or 0 for o in ore))}
    return out


def prossimo_weekend(wk, adesso):
    """Il weekend in corso o il prossimo: quello la cui gara è partita da meno di 14 ore."""
    for w in wk:
        t = ts(w["orari"]["Gara"])
        if t and t + 14 * 3600 > adesso:
            return w
    return wk[-1] if wk else None


def classifica_sessione(s):
    """Classifica di una sessione finita, per la pagina."""
    sg = model.sigle(s)
    gomme = {}
    for g in s.get("gomme") or []:
        if g[0] in sg:
            gomme.setdefault(sg[g[0]], []).append((g[1], g[4]))
    best = model.migliori_giri(s)
    soste = {}
    for p in s.get("pit") or []:
        if p[0] in sg:
            soste[sg[p[0]]] = soste.get(sg[p[0]], 0) + 1
    righe = []
    for x in sorted(s.get("risultato") or [], key=lambda x: (x.get("pos") or 99)):
        sig = sg.get(x["n"])
        if not sig:
            continue
        t = x.get("tempo")
        dist = x.get("distacco")
        righe.append({"pos": x.get("pos"), "sigla": sig, "giri": x.get("giri"),
                      "tempo": t if not isinstance(t, list) else [v for v in t],
                      "distacco": dist, "best": best.get(sig), "fuori": bool(x.get("dnf") or x.get("dns") or x.get("dsq")),
                      "stato": "squalificato" if x.get("dsq") else "non partito" if x.get("dns") else "ritirato" if x.get("dnf") else None,
                      "gomme": [c for _, c in sorted(gomme.get(sig, []))], "soste": soste.get(sig, 0),
                      "punti": x.get("punti")})
    sc = sum(1 for e in s.get("rc") or [] if e[2] == "SafetyCar" and (e[4] or "").upper().startswith("SAFETY CAR DEPLOYED"))
    vsc = sum(1 for e in s.get("rc") or [] if e[2] == "SafetyCar" and ("VSC DEPLOYED" in (e[4] or "").upper()
                                                                       or "VIRTUAL SAFETY CAR DEPLOYED" in (e[4] or "").upper()))
    rosse = sum(1 for e in s.get("rc") or [] if e[3] == "RED")
    pen = [e[4] for e in s.get("rc") or [] if "PENALTY" in (e[4] or "") and "SERVED" not in (e[4] or "")][-8:]
    return {"nome": model.NOMI_SESSIONE.get(s["nome"], s["nome"]), "sk": s["sk"], "inizio": s["inizio"],
            "righe": righe, "sc": sc, "vsc": vsc, "rosse": rosse, "penalita": pen, "meteo": s.get("meteo")}


def mini_mappa(t, punti=90):
    """Il tracciato in miniatura per il calendario: percorso SVG in un riquadro 100×100, stesso orientamento della
    mappa grande."""
    if not t or not t.get("x"):
        return None
    a = math.radians(t.get("rotazione") or 0)
    c, s = math.cos(a), math.sin(a)
    pts = [(x * c - y * s, -(x * s + y * c)) for x, y in zip(t["x"], t["y"])]
    pts = pts[::max(1, len(pts) // punti)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    k = 92 / max(w, h, 1)
    ox, oy = (100 - w * k) / 2 - min(xs) * k, (100 - h * k) / 2 - min(ys) * k
    return "M" + "L".join(f"{x * k + ox:.1f},{y * k + oy:.1f}" for x, y in pts) + "Z"


def calendario(st, wk, sess):
    vinc = {int(g["round"]): g["Results"] for g in st.get("risultati") or []}
    qual = {int(g["round"]): g["QualifyingResults"] for g in st.get("qualifiche") or []}
    spr = {int(g["round"]): g["SprintResults"] for g in st.get("sprint") or []}
    out = []
    for w in wk:
        rd = w["round"]
        ris = vinc.get(rd) or []
        out.append({
            "round": rd, "nome": w["nome"], "gp": w["gp"], "circuito": w["circuito"], "luogo": w["luogo"],
            "paese": w["paese"], "orari": w["orari"], "sprint": w["sprint"], "cittadino": w["cittadino"],
            "vincitore": ris[0]["Driver"]["code"] if ris else None,
            "pole": qual[rd][0]["Driver"]["code"] if qual.get(rd) else None,
            "sprint_vincitore": spr[rd][0]["Driver"]["code"] if spr.get(rd) else None,
            "arrivo": [[int(x["position"]), x["Driver"]["code"], int(x["grid"]), x["status"], float(x["points"])]
                       for x in ris],
            "inizio": w["inizio"],
            "mappa": mini_mappa(leggi(CACHE / "tracciati" / f"{w['ck']}.json")) if w.get("ck") else None,
        })
    return out


def calcola(adesso=None, ritara=False):
    """Ricalcola tutto e scrive la fotografia per la pagina e per il sito."""
    adesso = adesso or time.time()
    t0 = time.time()
    st = leggi(FILE_STAGIONE)
    if not st:
        return None
    sess = sessioni_della_stagione(st)
    storia = leggi(FILE_STORIA, {}) or {}
    # le gare 2026 già corse entrano nella storia dei circuiti
    storia = dict(storia, gare=list(storia.get("gare", [])) + gare_2026_per_storia(st, sess))
    wk = model.weekend(st)
    misure = model.Misure(wk, sess)
    w = prossimo_weekend(wk, adesso)
    meteo = leggi(FILE_METEO)
    contesto = {"st": st, "wk": wk, "sess": sess, "storia": storia, "misure": misure,
                "pioggia": pioggia_eventi(meteo, w) if w else {}}

    # taratura delle incertezze: quando c'è una gara nuova
    finiti = len(model.eventi_finiti(contesto))
    par_file = leggi(FILE_PARAMETRI) or {}
    if ritara or par_file.get("eventi") != finiti:
        log("Taratura del modello sulle gare già corse...")
        par, info = model.tara(contesto, partenza=par_file.get("par"))
        par_file = {"par": par, "eventi": finiti, "info": info, "at": adesso}
        scrivi(FILE_PARAMETRI, par_file)
        log(f"Taratura: {par}")
    par = par_file.get("par") or dict(model.PARAMETRI)

    val = model.valuta(misure, adesso, w["cittadino"] if w else None)
    stat = model.statistiche(contesto, model.valuta(misure, adesso))
    stat_sq = {s["squadra"]: s for s in stat["squadre"]}

    prossimo = None
    if w:
        circ = model.storia_circuito(storia, w["ck"], w["cittadino"])
        pron = {}
        for ev, nome in (("Sprint", "Sprint"), ("Race", "Gara")):
            if ev == "Sprint" and not w["sprint"]:
                continue
            sk = w["sessioni"].get(ev)
            if sk in sess and sess[sk].get("risultato"):
                continue   # già corsa
            try:
                p = model.previsione_evento(contesto, w, ev, adesso, par, n_sim=8000, seed=w["round"])
            except Exception as e:   # un dato strano di una fonte non deve fermare tutta la pagina
                log(f"Pronostico {nome} non calcolato: {type(e).__name__}: {e}")
                gh_note(f"Pronostico {nome} non calcolato: {type(e).__name__}: {e}", "warning")
                STATO["errori"]["pronostico"] = f"{type(e).__name__}: {e}"
                continue
            if not p["prev"]:
                continue
            pron[nome] = {
                "_prev": p["prev"],
                "piloti": sorted([dict(v, sigla=s) for s, v in p["prev"].items()], key=lambda x: -x["vince"] - x["podio"] / 10),
                "griglia": p["griglia"], "usato": p["usato"], "pioggia": p["pioggia"],
                "perche": model.perche(p["prev"], p["val"], circ, w, ev, p["usato"], p["griglia"], p["pioggia"], stat_sq),
                "squadre": punti_squadre(p["prev"]),
            }
        aperti = {(w["round"], "Race" if nome == "Gara" else "Sprint"): x for nome, x in pron.items()}
        sessioni_fatte = []
        for nome in ("Practice 1", "Practice 2", "Practice 3", "Sprint Qualifying", "Sprint", "Qualifying", "Race"):
            sk = w["sessioni"].get(nome)
            if sk in sess and sess[sk].get("risultato"):
                sessioni_fatte.append(classifica_sessione(sess[sk]))
        tracc = leggi(CACHE / "tracciati" / f"{w['ck']}.json") if w.get("ck") else None
        prossimo = {**{k: w[k] for k in ("round", "nome", "gp", "circuito", "luogo", "paese", "orari", "sprint",
                                         "cittadino", "tipo_circuito", "sessioni")},
                    "circ": circ, "tracciato": tracc, "meteo": meteo_sessioni(meteo, w), "pronostici": pron,
                    "risultati": sessioni_fatte, "strategia": strategia(circ, tracc, stat)}

    storico = aggiorna_storico(contesto, par, adesso, aperti if w else {}, w)
    for x in (prossimo or {}).get("pronostici", {}).values():
        x.pop("_prev", None)

    def val_futuro(wf):
        v = model.valuta(misure, adesso, wf["cittadino"])["piloti"]
        if w and wf["round"] == w["round"]:
            v, _ = model.aggiorna_con_weekend({"piloti": v}, misure, wf["round"], adesso)
        return model.solo_chi_corre(v, wf, sess, adesso)

    ritiro, base = model.prob_ritiro(st.get("risultati") or [], misure, 99)
    titolo = model.simula_titolo(st, wk, val_futuro, storia, par, ritiro, base, n_sim=1500 if CLOUD else 2500,
                                 misure=misure, sess=sess)

    # analisi di ogni round già corso: una pagina a sé, caricata solo quando si apre
    tutte = []
    for wx in wk:
        try:
            a = analisi.analisi_round(wx, sess, st)
        except Exception as e:   # un dato strano di una sessione non deve fermare il resto
            log(f"Analisi del round {wx['round']} non riuscita: {type(e).__name__}: {e}")
            continue
        if a:
            tutte.append(a)
    STATO["gare"] = {a["round"]: a for a in tutte}
    squadra_cid = {}
    for g in st.get("risultati") or []:
        for x in g["Results"]:
            sq = misure.squadra.get((int(g["round"]), x["Driver"].get("code")))
            if sq:
                squadra_cid[x["Constructor"]["constructorId"]] = sq
    squadra_pil = {p["sigla"]: p["squadra"] for p in stat["piloti"]}
    duelli = analisi.duelli(misure)
    in_testa = {}
    for a in tutte:
        for r in (a.get("gara") or {}).get("righe", []):
            in_testa[r["sigla"]] = in_testa.get(r["sigla"], 0) + r["in_testa"]
    for p in stat["piloti"]:
        p["duello"] = duelli.get(p["sigla"], [])
        p["giri_in_testa"] = in_testa.get(p["sigla"], 0)
    riepiloghi = [x for x in (analisi.riepilogo(a) for a in tutte) if x]

    colori = {}
    for p in stat["piloti"]:
        if p.get("colore") and p.get("squadra"):
            colori[p["squadra"]] = "#" + p["colore"]
    foto = {
        "versione": VERSIONE, "generato": adesso, "anno": ANNO, "colori": colori,
        "prossimo": prossimo, "titolo": titolo, "calendario": calendario(st, wk, sess),
        "piloti": stat["piloti"], "squadre": stat["squadre"], "degrado_medio": stat["degrado_medio"],
        "pit_medio": stat["pit_medio"],
        "passo": {"squadre": {s: {k: model.r(v.get(k)) for k in ("q", "r", "adatt")} for s, v in val["squadre"].items()}},
        "storico": {"voci": storico[-80:], "riepilogo": model.riepilogo_storico(storico)},
        "ultimo": riepiloghi[-1] if riepiloghi else None,
        "gare_analizzate": sorted(STATO["gare"]),
        "progressione": analisi.progressione(st, squadra_cid),
        "record": analisi.record(tutte, squadra_pil),
        "modello": {"parametri": par, "tarato_su": par_file.get("eventi")},
        "fonti": {"errori": dict(STATO["errori"]), "stagione_at": st.get("at"),
                  "openf1_in_pausa": bool(STATO.get("openf1_in_pausa"))},
    }
    STATO["foto"] = foto
    STATO["contesto"] = contesto
    STATO["par"] = par
    scrivi(FILE_FOTO, foto)
    log(f"Calcolo fatto in {time.time() - t0:.1f} s")
    return foto


def gare_2026_per_storia(st, sess):
    """Le gare 2026 già corse nella stessa forma della storia dei circuiti."""
    out = []
    jr = {g["date"]: g["Results"] for g in st.get("risultati") or []}
    for s in sess.values():
        if s["nome"] != "Race" or not s.get("risultato"):
            continue
        ss = next((x for x in st.get("sessioni") or [] if x["session_key"] == s["sk"]), None)
        if not ss:
            continue
        jres = jr.get(ss["date_start"][:10]) or []
        rc = [{"category": e[2], "flag": e[3], "message": e[4]} for e in s.get("rc") or []]
        gomme = [{"driver_number": g[0], "stint_number": g[1], "lap_start": g[2], "lap_end": g[3], "compound": g[4]}
                 for g in s.get("gomme") or []]
        meteo = [{"rainfall": 1 if (s.get("meteo") or {}).get("pioggia") else 0}]
        out.append(dati.riassunto_gara(ss, rc, gomme, meteo, jres))
    return out


def punti_squadre(prev):
    out = {}
    for s, p in prev.items():
        sq = p.get("squadra")
        out[sq] = out.get(sq, 0) + p["pt"]
    return sorted([[k, round(v, 1)] for k, v in out.items()], key=lambda x: -x[1])


def strategia(circ, tracc, stat):
    soste = circ.get("soste") or {}
    tot = sum(soste.values()) or 1
    piu = max(soste, key=soste.get) if soste else None
    nomi = {"S": "Morbida", "M": "Media", "H": "Dura", "I": "Intermedia", "W": "Bagnato"}
    return {"soste": {str(k): v / tot for k, v in sorted(soste.items(), key=lambda x: int(x[0]))},
            "tipica": int(piu) if piu is not None else None,
            "sequenze": [["–".join(nomi.get(c, c) for c in seq.split("-")), n] for seq, n in circ.get("sequenze") or []],
            "pit_perso": (tracc or {}).get("pit"), "degrado": stat.get("degrado_medio")}


# ======================================================================= storico dei pronostici

def aggiorna_storico(contesto, par, adesso, aperti, w_aperto):
    """Lo storico tiene, per ogni sprint e gara, l'ultimo pronostico fatto prima del via e poi l'arrivo vero.
    Le gare corse prima che l'app esistesse vengono ricostruite con i soli dati disponibili prima del via
    (e segnate come «ricostruite»)."""
    storico = leggi(FILE_STORICO, []) or []
    per = {(v["round"], v["evento"]): v for v in storico}
    cambiato = False
    for w in contesto["wk"]:
        for ev in ("Sprint", "Race"):
            sk = w["sessioni"].get(ev)
            if not sk:
                continue
            s = contesto["sess"].get(sk)
            inizio = ts(w["orari"].get("Gara" if ev == "Race" else "Sprint"))
            v = per.get((w["round"], ev))
            if s and s.get("risultato"):
                ordine, fuori = model.risultato_ordinato(s)
                if v and not v.get("ordine"):
                    v["ordine"], v["fuori"] = ordine, sorted(fuori)
                    cambiato = True
                elif not v and w["round"] >= 3:
                    p = model.previsione_evento(contesto, w, ev, s["inizio"], par, n_sim=3000, seed=w["round"])
                    if p["prev"]:
                        v = model.voce_storico(w, ev, p["prev"], ordine, fuori, ricostruita=True, at=s["inizio"],
                                               griglia=p["griglia"])
                        per[(w["round"], ev)] = v
                        cambiato = True
            elif inizio and adesso < inizio and (w["round"], ev) in aperti:
                # pronostico ancora aperto: si tiene l'ultimo prima del via, lo stesso che mostra la pagina
                x = aperti[(w["round"], ev)]
                per[(w["round"], ev)] = model.voce_storico(w, ev, x["_prev"], at=adesso, griglia=x["griglia"])
                cambiato = True
    storico = sorted(per.values(), key=lambda v: (v["round"], v["evento"] != "Sprint"))
    if cambiato:
        scrivi(FILE_STORICO, storico)
    return storico


# ======================================================================= aggiornamento delle fonti

async def aggiorna_fonti(f, adesso, forza=False):
    """Un giro di aggiornamento: restituisce True se qualcosa è cambiato."""
    cambiato = False
    st = leggi(FILE_STAGIONE)
    wk = model.weekend(st) if st else []
    w = prossimo_weekend(wk, adesso) if wk else None
    in_weekend = bool(w and w["inizio"] and ts(w["inizio"]) - 3600 < adesso)
    ogni = 10 * 60 if in_weekend else 30 * 60
    if forza or not st or adesso - st.get("at", 0) > ogni:
        try:
            nuovo = await dati.stagione(f, ANNO, st)
            scrivi(FILE_STAGIONE, nuovo)
            st = nuovo
            cambiato = True
            STATO["errori"].pop("calendario", None)
        except (RuntimeError, NonDisponibile) as e:
            STATO["errori"]["calendario"] = str(e)
            log(f"Calendario non aggiornato: {e}")
    if not st:
        return cambiato
    try:
        n = await dati.aggiorna_sessioni(f, st["sessioni"], adesso, limite=None if CLOUD else 6)
        cambiato |= n > 0
    except (RuntimeError, NonDisponibile) as e:
        STATO["errori"]["sessioni"] = str(e)
    storia = leggi(FILE_STORIA)
    if not storia or any(str(a) not in storia.get("anni", []) for a in dati.ANNI_STORIA):
        try:
            scrivi(FILE_STORIA, await dati.storia_circuiti(f, storia))
            cambiato = True
        except (RuntimeError, NonDisponibile) as e:
            STATO["errori"]["storia"] = str(e)
    wk = model.weekend(st)
    w = prossimo_weekend(wk, adesso)
    # i tracciati di tutti i circuiti: il prossimo per la mappa grande, gli altri per le miniature del calendario
    for wx in sorted(wk, key=lambda x: x is not w):
        if wx.get("ck") and not (CACHE / "tracciati" / f"{wx['ck']}.json").exists():
            if await dati.tracciato(f, wx["ck"], ANNO):
                cambiato = True
                continue
            gara = dati.leggi(dati.file_sessione(wx["sessioni"].get("Race"))) if wx["sessioni"].get("Race") else None
            if gara and gara.get("risultato"):   # MultiViewer non ce l'ha: dalla gara già corsa
                try:
                    if await dati.tracciato_da_openf1(f, wx["ck"], gara, ANNO):
                        cambiato = True
                except (RuntimeError, NonDisponibile):
                    pass
    if w and w.get("ck"):
        meteo = leggi(FILE_METEO)
        t_gara = ts(w["orari"]["Gara"])
        if t_gara - adesso < 8 * 86400 and (not meteo or meteo.get("round") != str(w["round"])
                                            or adesso - meteo.get("at", 0) > 3 * 3600):
            jg = next((g for g in st["gare"] if int(g["round"]) == w["round"]), None)
            try:
                scrivi(FILE_METEO, await dati.meteo_weekend(f, jg))
                cambiato = True
                STATO["errori"].pop("meteo", None)
            except RuntimeError as e:
                STATO["errori"]["meteo"] = str(e)
    STATO["openf1_in_pausa"] = f.openf1_in_pausa
    STATO["errori"].update({k: v for k, v in f.errori.items()})
    for k in ("OpenF1", "Jolpica", "Open-Meteo"):
        if k not in f.errori:
            STATO["errori"].pop(k, None)
    return cambiato


# ======================================================================= live

class Live:
    """Durante una sessione legge da OpenF1 posizioni, distacchi, giri, gomme, box e direzione gara."""

    def __init__(self):
        self.reset(None)

    def reset(self, s):
        self.s = s
        self.sk = s["session_key"] if s else None
        self.piloti, self.pos, self.int, self.giri, self.gomme, self.rc, self.meteo = {}, {}, {}, {}, [], [], None
        self.ultimo = {}
        self.errore = None
        self.at = None
        self.n = 0
        self.prossimo = 0

    async def giro(self, f):
        sk = self.sk
        gara = self.s["session_name"] in ("Race", "Sprint")
        try:
            if not self.piloti:
                for p in await f.openf1("drivers", session_key=sk):
                    self.piloti[p["driver_number"]] = {"sigla": p.get("name_acronym"), "squadra": p.get("team_name"),
                                                       "colore": p.get("team_colour")}
            for x in await f.openf1("position", session_key=sk, **self._da("pos")):
                self.pos[x["driver_number"]] = x["position"]
                self.ultimo["pos"] = x["date"]
            if gara:
                for x in await f.openf1("intervals", session_key=sk, **self._da("int")):
                    self.int[x["driver_number"]] = (x.get("gap_to_leader"), x.get("interval"))
                    self.ultimo["int"] = x["date"]
            for x in await f.openf1("laps", session_key=sk, **self._da("giri", "date_start")):
                self.giri.setdefault(x["driver_number"], {})[x["lap_number"]] = [
                    x.get("lap_duration"), x.get("duration_sector_1"), x.get("duration_sector_2"),
                    x.get("duration_sector_3"), x.get("is_pit_out_lap")]
                if x.get("date_start"):
                    self.ultimo["giri"] = iso(ts(x["date_start"]) - 240)   # i giri si completano dopo
            if self.n % 2 == 0:
                self.gomme = await f.openf1("stints", session_key=sk)
            for x in await f.openf1("race_control", session_key=sk, **self._da("rc")):
                self.rc.append([ts(x["date"]), x.get("lap_number"), x.get("category"), x.get("flag"),
                                x.get("message"), x.get("driver_number")])
                self.ultimo["rc"] = x["date"]
            if self.n % 6 == 0:
                m = await f.openf1("weather", session_key=sk, **self._da("meteo"))
                if m:
                    self.meteo = m[-1]
                    self.ultimo["meteo"] = m[-1]["date"]
            self.errore = None if self.piloti else ("OpenF1 non dà ancora dati per questa sessione: senza abbonamento "
                                                    "arrivano a sessione finita, nella scheda Weekend.")
            self.at = time.time()
            self.n += 1
        except NonDisponibile as e:
            self.errore = ("Il live non è disponibile: durante la sessione OpenF1 dà i dati solo agli abbonati "
                           "(9,90 € al mese). A sessione finita i risultati compaiono nella scheda Weekend."
                           if not f.abbonato else f"OpenF1 non ha dato i dati live: {e}")
        except RuntimeError as e:
            self.errore = str(e)

    def _da(self, chiave, campo="date"):
        v = self.ultimo.get(chiave)
        return {campo + "__gt": v} if v else {}

    def stato(self):
        if not self.s:
            return None
        gara = self.s["session_name"] in ("Race", "Sprint")
        righe = []
        giro_max = 0
        for n, p in self.piloti.items():
            gs = self.giri.get(n, {})
            completi = {k: v for k, v in gs.items() if v[0]}
            ultimo_n = max(gs) if gs else 0
            giro_max = max(giro_max, ultimo_n)
            ult = completi[max(completi)] if completi else None
            best = min((v[0] for v in completi.values() if not v[4]), default=None)
            st = [g for g in self.gomme if g["driver_number"] == n]
            st.sort(key=lambda g: g["stint_number"])
            att = st[-1] if st else None
            eta = None
            if att and att.get("lap_start") is not None:
                eta = (att.get("tyre_age_at_start") or 0) + max(0, ultimo_n - att["lap_start"])
            gap, inter = self.int.get(n, (None, None))
            righe.append({"n": n, "sigla": p["sigla"], "squadra": p["squadra"], "colore": p["colore"],
                          "pos": self.pos.get(n), "gap": gap, "int": inter, "giro": ultimo_n,
                          "ultimo": ult[0] if ult else None, "settori": ult[1:4] if ult else None, "best": best,
                          "gomma": att.get("compound") if att else None, "eta": eta, "soste": max(0, len(st) - 1)})
        if gara:
            righe.sort(key=lambda x: x["pos"] or 99)
        else:
            righe.sort(key=lambda x: (x["best"] is None, x["best"] or 0, x["pos"] or 99))
            if righe and righe[0]["best"]:
                for x in righe:
                    x["gap"] = round(x["best"] - righe[0]["best"], 3) if x["best"] else None
        msgs = [{"t": e[0], "giro": e[1], "msg": e[4], "flag": e[3]} for e in self.rc[-12:]][::-1]
        neutral = None
        for e in self.rc:
            m = (e[4] or "").upper()
            if e[2] == "SafetyCar" and "DEPLOYED" in m:
                neutral = "VSC" if "VSC" in m or "VIRTUAL" in m else "SC"
            elif e[2] == "SafetyCar" and ("ENDING" in m or "IN THIS LAP" in m):
                neutral = None
            if e[3] == "RED":
                neutral = "Bandiera rossa"
            if e[3] == "GREEN" and neutral == "Bandiera rossa":
                neutral = None
        out = {"sk": self.sk, "nome": model.NOMI_SESSIONE.get(self.s["session_name"], self.s["session_name"]),
               "gp": self.s.get("circuit_short_name"), "inizio": ts(self.s["date_start"]), "fine": ts(self.s["date_end"]),
               "gara": gara, "giro": giro_max, "righe": righe, "messaggi": msgs, "neutralizzata": neutral,
               "meteo": self.meteo, "errore": self.errore, "at": self.at}
        if gara:
            out["probabilita"] = prob_live(out, self.s["session_name"])
        return out


def prob_live(stato, evento):
    """Probabilità di vittoria e podio a gara in corso: distacchi di adesso più il passo dei piloti per i giri che
    restano, le soste ancora da fare, la safety car e i ritiri."""
    foto, contesto = STATO.get("foto"), STATO.get("contesto")
    if not foto or not foto.get("prossimo") or stato["giro"] < 2:
        return None
    w = foto["prossimo"]
    circ = w["circ"]
    totali = None
    gare_qui = [g for g in (contesto["storia"].get("gare") or []) if g["circuito"] == next(
        (x["ck"] for x in contesto["wk"] if x["round"] == w["round"]), None) and g["tipo"] == evento and g.get("giri")]
    if gare_qui:
        totali = max(set(g["giri"] for g in gare_qui), key=[g["giri"] for g in gare_qui].count)
    if not totali:
        return None
    restano = max(0, totali - stato["giro"])
    frazione = restano / totali
    tempi = [x["ultimo"] for x in stato["righe"] if x["ultimo"]]
    giro_t = model.mediana(tempi) or 95
    pron = (w["pronostici"].get("Gara" if evento == "Race" else "Sprint") or {}).get("piloti") or []
    passo = {p["sigla"]: p["r"] for p in pron}
    tr = w.get("tracciato") or {}
    pit = tr.get("pit") if isinstance(tr.get("pit"), (int, float)) else 22
    if stato.get("neutralizzata") in ("SC", "VSC") and isinstance(tr.get("pit_sc"), (int, float)):
        pit = tr["pit_sc"] if stato["neutralizzata"] == "SC" else (tr.get("pit_vsc") or pit)   # sotto SC la sosta costa meno
    soste_attese = 0 if evento == "Sprint" else max(1, w["strategia"].get("tipica") or 1)
    par = STATO.get("par") or model.PARAMETRI
    # stare davanti vale: per superare serve essere più veloci, tanto più quanto il circuito è difficile
    costo_pos = model.costo_griglia(circ, par["kg"]) * frazione
    rng = random.Random(stato["giro"])
    righe = sorted([x for x in stato["righe"] if x["pos"]], key=lambda x: x["pos"])
    conta = {x["sigla"]: [0, 0] for x in righe}
    n_sim = 3000
    for _ in range(n_sim):
        sc = rng.random() < circ["p_sc"] * frazione
        fine = {}
        for i, x in enumerate(righe):
            if rng.random() < 0.05 * frazione:
                continue
            gap = x["gap"]
            if isinstance(gap, str):   # doppiato
                gap = 999.0
            gap = gap or 0.0
            p = (passo.get(x["sigla"], 0.3) + rng.gauss(0, par["sr"] * max(frazione, 0.15) ** 0.5)) / 100 * giro_t * restano
            debito = pit if (x["soste"] < soste_attese and restano > 3) else 0
            tot = gap + p + debito
            if sc:
                tot = tot * 0.5 + rng.gauss(0, 3)
            fine[x["sigla"]] = tot + i * costo_pos + rng.gauss(0, 1.0)
        ordine = sorted(fine, key=fine.get)
        for i, s in enumerate(ordine[:3]):
            if i == 0:
                conta[s][0] += 1
            conta[s][1] += 1
    return {"giri_totali": totali, "restano": restano,
            "piloti": sorted([{"sigla": s, "vince": c[0] / n_sim, "podio": c[1] / n_sim} for s, c in conta.items()],
                             key=lambda x: (-x["vince"], -x["podio"]))[:10]}


def sessione_in_corso(st, adesso):
    for s in st.get("sessioni") or []:
        if s.get("is_cancelled") or s["session_name"].startswith("Day"):
            continue
        a, b = ts(s["date_start"]), ts(s["date_end"])
        if a - 10 * 60 <= adesso <= b + 20 * 60:
            return s
    return None


# ======================================================================= ciclo principale (Mac)

async def ciclo():
    live = Live()
    STATO["live_obj"] = live
    async with Fonti() as f:
        ultimo_calcolo = 0
        prossimo_giro = 0
        while True:
            adesso = time.time()
            st = leggi(FILE_STAGIONE) or {}
            s = sessione_in_corso(st, adesso) if st else None
            if s:
                if s["session_key"] != live.sk:
                    live.reset(s)
                    log(f"Sessione in corso: {s['circuit_short_name']} {s['session_name']}")
                if adesso >= live.prossimo:
                    await live.giro(f)
                    # senza dati (niente abbonamento) si riprova ogni 2 minuti, per non consumare le richieste
                    live.prossimo = time.time() + (3 if live.at and not live.errore else 120)
                    STATO["live"] = live.stato()
            elif live.sk:
                live.reset(None)
                STATO["live"] = None
            if adesso >= prossimo_giro:
                try:
                    cambiato = await aggiorna_fonti(f, adesso)
                except Exception as e:   # un errore inatteso non deve fermare l'app
                    log(f"Aggiornamento non riuscito: {type(e).__name__}: {e}")
                    cambiato = False
                prossimo_giro = adesso + (120 if s else 300)
                if cambiato or adesso - ultimo_calcolo > 30 * 60:
                    try:
                        await asyncio.get_running_loop().run_in_executor(None, calcola)
                        await pubblica_sito_dal_mac()
                    except Exception as e:   # un dato strano di una fonte non deve chiudere l'app: si riprova al giro dopo
                        log(f"Calcolo non riuscito: {type(e).__name__}: {e}")
                        STATO["errori"]["calcolo"] = f"{type(e).__name__}: {e}"
                    else:
                        STATO["errori"].pop("calcolo", None)
                    ultimo_calcolo = time.time()
            await asyncio.sleep(1 if s else 30)


# ======================================================================= sito

def conf_sito():
    return leggi(DATA / "sito.json")


async def pubblica_sito_dal_mac():
    conf = conf_sito()
    if not conf or conf.get("cloud"):
        return   # il sito lo aggiorna GitHub Actions
    foto = STATO.get("foto")
    if not foto:
        return

    def lavoro():
        sito.collega(SITE_DIR, conf["remote"])
        sito.prepara(SITE_DIR, foto, STATO.get("gare"))
        return sito.pubblica(SITE_DIR, conf.get("branches") or ["sito"])
    ok, out = await asyncio.get_running_loop().run_in_executor(None, lavoro)
    log("Sito aggiornato su GitHub" if ok else f"Sito non aggiornato: {out[-200:]}")


async def cloud_main():
    """Un aggiornamento su GitHub Actions: fonti, calcolo, sito, salvataggio dei dati."""
    inizio = time.time()
    async with Fonti() as f:
        await aggiorna_fonti(f, inizio, forza=True)
    foto = calcola()
    if not foto:
        gh_note("Mancano i dati della stagione: niente da pubblicare")
        return 1
    errori = dict(STATO["errori"])
    foto["fonti"]["errori"] = errori
    sito.prepara(SITE_DIR, foto, STATO.get("gare"))
    ok_s, out = sito.pubblica(SITE_DIR, ["sito"])
    log("Sito pubblicato" if ok_s else f"Sito non pubblicato: {out[-300:]}")
    if not ok_s:
        gh_note(f"Sito non pubblicato: {out[-300:]}")
    scrivi(FILE_ESECUZIONE, {"at": time.time(), "inizio": inizio, "errori": errori, "versione": VERSIONE,
                             "prossimo": prossime_sessioni()})
    for tentativo in (1, 2, 3):
        ok, out = sito.pubblica(DATA, ["dati"], lease=True)
        if ok or "stale info" in out or tentativo == 3:
            break
        time.sleep(20)
    log("Dati salvati su GitHub" if ok else f"Dati non salvati: {out[-300:]}")
    if os.environ.get("GITHUB_ACTIONS") and FILE_STORICO.exists():
        oggi = time.strftime("%Y-%m-%d")
        if EXTRA.get("storico_giorno") != oggi:
            ok_c, msg = sito.copia_storico(DATA, FILE_STORICO, f"Storico dei pronostici del {time.strftime('%d/%m/%Y')}")
            if ok_c:
                EXTRA["storico_giorno"] = oggi
                salva_extra()
            log(msg)
    return 0 if ok and ok_s else 1


def prossime_sessioni():
    """Inizio e fine delle sessioni dei prossimi 10 giorni: GitHub le usa per aggiornare più spesso nei weekend."""
    st = leggi(FILE_STAGIONE) or {}
    adesso = time.time()
    return [[ts(s["date_start"]), ts(s["date_end"])] for s in st.get("sessioni") or []
            if not s.get("is_cancelled") and adesso - 86400 < ts(s["date_end"]) < adesso + 10 * 86400]


# ======================================================================= pagina

async def h_index(_):
    return web.FileResponse(ROOT / "pagina.html", headers={"Cache-Control": "no-cache"})


async def h_dati(_):
    if not STATO["foto"]:
        return web.json_response({"attesa": "Sto scaricando i dati della stagione: la prima volta ci vuole circa mezz'ora."},
                                 status=503)
    return web.json_response(STATO["foto"], dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def h_live(_):
    return web.json_response({"live": STATO.get("live"), "errori": STATO["errori"], "at": time.time()},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def h_gara(req):
    try:
        a = (STATO.get("gare") or {}).get(int(req.match_info["rd"]))
    except ValueError:
        a = None
    if not a:
        raise web.HTTPNotFound()
    return web.json_response(a, dumps=lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")))


async def h_versione(_):
    return web.json_response({"app": "f1-live", "versione": VERSIONE})


async def h_favicon(_):
    """Icona della scheda del browser: «F1» bianco su rosso, disegnata per leggersi anche a 16 pixel."""
    p = ROOT / "favicon.png"
    if not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})


async def h_icona(_):
    """Icona dell'app per la schermata Home e il manifest (le stesse che pubblica il sito)."""
    p = ROOT / "icona.png"
    if not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})


async def h_manifest(_):
    return web.json_response(sito.MANIFEST, content_type="application/manifest+json")


async def versione_in_esecuzione():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            async with s.get(f"http://127.0.0.1:{PORT}/api/versione") as r:
                if r.status == 200:
                    return (await r.json()).get("versione")
    except Exception:
        pass
    return None


async def chiudi_vecchio():
    pids = subprocess.run(["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    for _ in range(40):
        await asyncio.sleep(.25)
        if not subprocess.run(["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.strip():
            return True
    return False


async def main():
    url = f"http://127.0.0.1:{PORT}/"
    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/api/dati", h_dati)
    app.router.add_get("/api/live", h_live)
    app.router.add_get("/api/gara/{rd}", h_gara)
    app.router.add_get("/api/versione", h_versione)
    app.router.add_get("/favicon.png", h_favicon)
    app.router.add_get("/favicon.ico", h_favicon)
    for n in (180, 192, 512):
        app.router.add_get(f"/icona-{n}.png", h_icona)
    app.router.add_get("/manifest.webmanifest", h_manifest)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
    except OSError:
        altro = await versione_in_esecuzione()
        if altro and altro != VERSIONE and await chiudi_vecchio():
            print("Chiusa la versione precedente di F1 Live: avvio quella aggiornata.", flush=True)
            await web.TCPSite(runner, "127.0.0.1", PORT).start()
        else:
            print(f"F1 Live è già in esecuzione su {url}" if altro else
                  f"La porta {PORT} è usata da un altro programma: chiudilo e riprova.", flush=True)
            if altro and "--no-open" not in sys.argv:
                webbrowser.open(url)
            return
    STATO["foto"] = leggi(FILE_FOTO)
    if STATO["foto"]:
        log("Caricati gli ultimi dati salvati")
    print(f"F1 Live pronto su {url}  (chiudi questa finestra o premi Ctrl+C per fermarlo)", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    if "--offline" in sys.argv:
        await asyncio.get_running_loop().run_in_executor(None, calcola)
        await asyncio.Event().wait()
    else:
        await ciclo()


if __name__ == "__main__":
    try:
        if CLOUD:
            try:
                sys.exit(asyncio.run(cloud_main()))
            except Exception as e:
                gh_note(f"Aggiornamento interrotto da un errore: {type(e).__name__}: {e}"[:500])
                raise
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
