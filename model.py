"""Tutto il calcolo di F1 Live: passo di piloti e squadre, pronostici, titolo, statistiche e storico.

Le misure di partenza sono sempre distacchi in percentuale dalla mediana del gruppo nella stessa sessione, così
piste, carburante e evoluzione della pista si annullano:
- qualifica: il miglior giro di ogni pilota nella sessione (Q1, Q2 o Q3);
- gara: giro per giro, il distacco dal tempo mediano di quel giro, solo nei giri puliti (niente primo giro, giri
  di entrata e uscita dai box, safety car, virtual, bandiere rosse, giri oltre il 5% più lenti); poi la mediana;
- libere: miglior giro e, sulle serie di almeno 5 giri, il passo sul passo lungo (pesano poco: non si sa la benzina).
"""
import math
import random
import statistics
import time
from collections import Counter, defaultdict

PUNTI_GARA = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
PUNTI_SPRINT = [8, 7, 6, 5, 4, 3, 2, 1]
DURATA_GARA = 5500.0     # secondi di gara "verde": serve a trasformare i distacchi in % in secondi
DURATA_SPRINT = 1850.0
CARBURANTE = 0.055       # secondi al giro che la macchina guadagna perché si alleggerisce
RECENZA = 0.82           # peso di una gara rispetto alla successiva
RITIRO_BASE = 0.07       # 2026, regolamento nuovo: più ritiri del solito
PARAMETRI = {"sq": 0.3, "sr": 0.4, "kg": 3.0, "s1": 4.0, "sw": 0.15}   # sostituiti dalla taratura sulle gare 2026


def media(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def mediana(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def wmedia(coppie):
    """Media pesata di coppie (valore, peso)."""
    sw = sum(w for _, w in coppie)
    return sum(v * w for v, w in coppie) / sw if sw else None


def it(x, nd=2):
    """Numero all'italiana, con la virgola."""
    return f"{x:.{nd}f}".replace(".", ",")


def r(x, nd=3):
    return None if x is None else round(x, nd)


# ======================================================================= weekend

def weekend(st):
    """I weekend della stagione: Jolpica (round, nomi, orari, risultati) unito a OpenF1 (sessioni e circuito)."""
    riunioni = {m["meeting_key"]: m for m in st.get("riunioni") or []}
    sess_per_mk = defaultdict(dict)
    for s in st.get("sessioni") or []:
        if not s.get("is_cancelled"):
            sess_per_mk[s["meeting_key"]][s["session_name"]] = s
    mk_per_data = {}
    for mk, ss in sess_per_mk.items():
        if "Race" in ss:
            mk_per_data[ss["Race"]["date_start"][:10]] = mk
    out = []
    for g in st.get("gare") or []:
        mk = mk_per_data.get(g["date"])
        m = riunioni.get(mk) or {}
        orari = {}
        for k, nome in (("FirstPractice", "Libere 1"), ("SecondPractice", "Libere 2"), ("ThirdPractice", "Libere 3"),
                        ("SprintQualifying", "Qualifica sprint"), ("Sprint", "Sprint"), ("Qualifying", "Qualifica")):
            if k in g:
                orari[nome] = g[k]["date"] + "T" + g[k].get("time", "00:00:00Z")
        orari["Gara"] = g["date"] + "T" + g.get("time", "00:00:00Z")
        out.append({
            "round": int(g["round"]), "nome": nome_gp(g["raceName"]), "gp": "GP " + nome_gp(g["raceName"]),
            "circuito": g["Circuit"]["circuitName"], "circuitId": g["Circuit"]["circuitId"],
            "luogo": g["Circuit"]["Location"]["locality"], "paese": g["Circuit"]["Location"]["country"],
            "lat": g["Circuit"]["Location"]["lat"], "lon": g["Circuit"]["Location"]["long"],
            "mk": mk, "ck": m.get("circuit_key"), "cittadino": "Street" in (m.get("circuit_type") or ""),
            "tipo_circuito": m.get("circuit_type"), "sprint": "Sprint" in g, "orari": orari,
            "sessioni": {nome: s["session_key"] for nome, s in sess_per_mk.get(mk, {}).items()},
            "inizio": min(sess_per_mk[mk][n]["date_start"] for n in sess_per_mk[mk]) if mk in sess_per_mk else None,
        })
    return out


NOMI_GP = {
    "Australian": "Australia", "Chinese": "Cina", "Japanese": "Giappone", "Bahrain": "Bahrain", "Saudi Arabian": "Arabia Saudita",
    "Miami": "Miami", "Emilia Romagna": "Emilia-Romagna", "Canadian": "Canada", "Monaco": "Monaco", "Barcelona": "Barcellona",
    "Spanish": "Spagna", "Austrian": "Austria", "British": "Gran Bretagna", "Belgian": "Belgio", "Hungarian": "Ungheria",
    "Dutch": "Olanda", "Italian": "Italia", "Azerbaijan": "Azerbaigian", "Singapore": "Singapore", "United States": "Stati Uniti",
    "Mexico City": "Città del Messico", "Brazilian": "Brasile", "São Paulo": "San Paolo", "Las Vegas": "Las Vegas",
    "Qatar": "Qatar", "Abu Dhabi": "Abu Dhabi", "Madrid": "Madrid",
}


def nome_gp(race_name):
    """«Bahrain Grand Prix in Malaysia» -> «Bahrain (in Malesia)», «Italian Grand Prix» -> «Italia»."""
    base, _, resto = race_name.partition(" Grand Prix")
    nome = NOMI_GP.get(base, base)
    resto = resto.strip()
    if resto:
        resto = resto.replace("in Malaysia", "in Malesia")
        nome += f" ({resto})"
    return nome


NOMI_SESSIONE = {"Practice 1": "Libere 1", "Practice 2": "Libere 2", "Practice 3": "Libere 3",
                 "Sprint Qualifying": "Qualifica sprint", "Sprint Shootout": "Qualifica sprint", "Sprint": "Sprint",
                 "Qualifying": "Qualifica", "Race": "Gara"}


# ======================================================================= misure per sessione

def sigle(sess):
    return {int(n): p["sigla"] for n, p in sess["piloti"].items() if p.get("sigla")}


def squadre(sess):
    return {p["sigla"]: p["squadra"] for p in sess["piloti"].values() if p.get("sigla")}


def migliori_giri(sess):
    """Miglior giro di ogni pilota: dal risultato (Q1, Q2, Q3) o, se manca, dai giri."""
    sg = sigle(sess)
    best = {}
    for x in sess.get("risultato") or []:
        t = x.get("tempo")
        vals = [v for v in (t if isinstance(t, list) else [t]) if v]
        if vals and x["n"] in sg and sess["tipo"] == "Qualifying":
            best[sg[x["n"]]] = min(vals)
    if len(best) < 10:
        for g in sess.get("giri") or []:
            if g[2] and not g[6] and g[0] in sg:
                s = sg[g[0]]
                best[s] = min(best.get(s, 1e9), g[2])
    return best


def distacchi_giro_secco(sess, tetto=3.0):
    best = migliori_giri(sess)
    if len(best) < 8:
        return {}
    m = mediana(best.values())
    return {s: (t / m - 1) * 100 for s, t in best.items() if (t / m - 1) * 100 < tetto}


def giri_puliti(sess, minimo=8):
    """Per ogni pilota la lista dei giri puliti come (giro, tempo, distacco % dalla mediana di quel giro)."""
    sg = sigle(sess)
    neutri = set(sess.get("neutralizzati") or [])
    box = {(p[0], p[1]) for p in sess.get("pit") or []}
    per_giro = defaultdict(list)
    for g in sess.get("giri") or []:
        n, lap, t = g[0], g[1], g[2]
        if not t or g[6] or lap is None or lap <= 1 or lap in neutri or (n, lap) in box or n not in sg:
            continue
        per_giro[lap].append((n, t))
    out = defaultdict(list)
    for lap, xs in per_giro.items():
        if len(xs) < 6:
            continue
        m = mediana([t for _, t in xs])
        for n, t in xs:
            d = (t / m - 1) * 100
            if d < 5:
                out[sg[n]].append((lap, t, d))
    return {s: v for s, v in out.items() if len(v) >= minimo}


def passo_gara(sess):
    minimo = 5 if sess["nome"] == "Sprint" else 8
    gp = giri_puliti(sess, minimo)
    if len(gp) < 8:
        return {}
    passo = {s: mediana([d for _, _, d in v]) for s, v in gp.items()}
    c = mediana(passo.values())
    return {s: p - c for s, p in passo.items()}


def passo_lungo_libere(sess):
    """Serie di almeno 5 giri consecutivi senza box, entro il 4% del migliore della serie."""
    sg = sigle(sess)
    per = defaultdict(list)
    for g in sess.get("giri") or []:
        if g[0] in sg:
            per[sg[g[0]]].append(g)
    passo = {}
    for s, gs in per.items():
        gs.sort(key=lambda g: g[1])
        serie, cur = [], []
        for g in gs:
            if g[6] or not g[2]:
                if len(cur) >= 5:
                    serie.append(cur)
                cur = []
                continue
            cur.append(g[2])
        if len(cur) >= 5:
            serie.append(cur)
        tempi = []
        for x in serie:
            b = min(x)
            buoni = [t for t in x if t < b * 1.04]
            if len(buoni) >= 5:
                tempi.extend(buoni)
        if len(tempi) >= 5:
            passo[s] = mediana(tempi)
    if len(passo) < 8:
        return {}
    m = mediana(passo.values())
    return {s: (t / m - 1) * 100 for s, t in passo.items() if abs(t / m - 1) * 100 < 4}


def degrado(sess):
    """Per ogni serie di gomme in gara: secondi persi a ogni giro di vita della gomma, al netto del carburante."""
    gp = giri_puliti(sess, 1)
    out = []
    for g in sess.get("gomme") or []:
        n, _, a, b, comp, eta = g
        s = sigle(sess).get(n)
        if not s or s not in gp or a is None or b is None or not comp:
            continue
        pts = [(lap, t) for lap, t, _ in gp[s] if a <= lap <= b]
        if len(pts) < 6:
            continue
        xs = [lap for lap, _ in pts]
        ys = [t for _, t in pts]
        mx, my = media(xs), media(ys)
        vx = sum((x - mx) ** 2 for x in xs)
        if vx <= 0:
            continue
        pend = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx + CARBURANTE
        if -0.15 < pend < 0.6:
            out.append({"sigla": s, "mescola": comp, "giri": len(pts), "degrado": pend})
    return out


def risultato_ordinato(sess):
    """Ordine d'arrivo come lista di sigle e insieme dei ritirati (DNF, non partiti, squalificati)."""
    sg = sigle(sess)
    ris = [x for x in sess.get("risultato") or [] if x["n"] in sg]
    arr = sorted([x for x in ris if x.get("pos")], key=lambda x: x["pos"])
    fuori = {sg[x["n"]] for x in ris if x.get("dnf") or x.get("dns") or x.get("dsq") or not x.get("pos")}
    ordine = [sg[x["n"]] for x in arr] + [sg[x["n"]] for x in ris if not x.get("pos")]
    return ordine, fuori


# ======================================================================= valutazioni

class Misure:
    """Tutte le misure delle sessioni finite, per weekend, calcolate una volta sola."""

    def __init__(self, wk, sess):
        self.wk = {w["round"]: w for w in wk}
        self.q, self.sq, self.r, self.sp, self.fp1, self.fpl = {}, {}, {}, {}, {}, {}
        self.deg = {}
        self.fine = {}     # (round, nome sessione) -> fine
        self.squadra = {}  # (round, sigla) -> squadra
        for w in wk:
            for nome, sk in w["sessioni"].items():
                s = sess.get(sk)
                if not s:
                    continue
                rd = w["round"]
                self.fine[(rd, nome)] = s["fine"]
                for sig, sq in squadre(s).items():
                    self.squadra[(rd, sig)] = sq
                if nome == "Qualifying":
                    self.q[rd] = distacchi_giro_secco(s)
                elif nome in ("Sprint Qualifying", "Sprint Shootout"):
                    self.sq[rd] = distacchi_giro_secco(s)
                elif nome == "Race":
                    self.r[rd] = passo_gara(s)
                    self.deg[rd] = degrado(s)
                elif nome == "Sprint":
                    self.sp[rd] = passo_gara(s)
                elif nome.startswith("Practice"):
                    self.fp1.setdefault(rd, []).append(distacchi_giro_secco(s, 4))
                    self.fpl.setdefault(rd, []).append(passo_lungo_libere(s))

    def finita(self, rd, nome, prima_di):
        f = self.fine.get((rd, nome))
        return f is not None and f < prima_di


def valuta(misure, prima_di, cittadino=None, solo_round_prima=None):
    """Passo in qualifica (q) e in gara (r) di squadre e piloti con i dati finiti prima di `prima_di`.
    Restituisce {"squadre": {sq: {q, r, n}}, "piloti": {sigla: {q, r, squadra, ...}}}."""
    rounds = sorted(rd for rd in misure.wk if (solo_round_prima is None or rd < solo_round_prima))
    fatti = [rd for rd in rounds if any(misure.finita(rd, n, prima_di) for n in ("Qualifying", "Race", "Sprint",
                                                                                   "Sprint Qualifying"))]
    eta = {rd: len(fatti) - 1 - i for i, rd in enumerate(fatti)}
    campioni = {"q": [], "r": []}   # (round, peso, sigla, squadra, distacco)
    for rd in fatti:
        base = RECENZA ** eta[rd]
        for nome, tab, chi, peso in (("Qualifying", misure.q, "q", 1.0), ("Sprint Qualifying", misure.sq, "q", 0.7),
                                     ("Race", misure.r, "r", 1.0), ("Sprint", misure.sp, "r", 0.55)):
            if not misure.finita(rd, nome, prima_di) or rd not in tab:
                continue
            for sig, d in tab[rd].items():
                sq = misure.squadra.get((rd, sig))
                if sq:
                    campioni[chi].append((rd, base * peso, sig, sq, d, nome))
    out_sq, out_pil = defaultdict(dict), defaultdict(dict)
    for chi, xs in campioni.items():
        per_sq = defaultdict(list)
        for rd, w, sig, sq, d, nome in xs:
            per_sq[sq].append((d, w))
        for sq, v in per_sq.items():
            out_sq[sq][chi] = wmedia(v)
            out_sq[sq]["n" + chi] = sum(w for _, w in v)
        # quanto ogni pilota sta sopra o sotto il compagno, nella stessa sessione
        media_sess = defaultdict(list)
        for rd, w, sig, sq, d, nome in xs:
            media_sess[(rd, nome, sq)].append(d)
        res = defaultdict(list)
        for rd, w, sig, sq, d, nome in xs:
            res[sig].append((d - media(media_sess[(rd, nome, sq)]), w))
        for sig, v in res.items():
            n = sum(w for _, w in v)
            out_pil[sig][chi + "_res"] = wmedia(v) * n / (n + 1.5)
    # squadra attuale di ogni pilota: l'ultima vista in qualifica o in gara (chi guida solo nelle libere 1 non conta)
    attuale = {}
    for chi in ("q", "r"):
        for rd, w, sig, sq, d, nome in sorted(campioni[chi]):
            if sig not in attuale or rd >= attuale[sig][0]:
                attuale[sig] = (rd, sq)
    attuale = {sig: sq for sig, (rd, sq) in attuale.items()}
    # tipo di pista: quanto ogni squadra va meglio o peggio sui circuiti cittadini
    adatt = {}
    if cittadino is not None:
        per = defaultdict(lambda: {True: [], False: []})
        for chi in ("q", "r"):
            for rd, w, sig, sq, d, nome in campioni[chi]:
                if out_sq[sq].get(chi) is not None:
                    per[sq][misure.wk[rd]["cittadino"]].append(d - out_sq[sq][chi])
        for sq, v in per.items():
            xs = v[cittadino]
            adatt[sq] = (media(xs) or 0) * len(xs) / (len(xs) + 6)
    for sig, sq in attuale.items():
        t = out_sq.get(sq, {})
        p = out_pil[sig]
        p["squadra"] = sq
        a = adatt.get(sq, 0)
        p["adatt"] = a
        if t.get("q") is not None:
            p["q"] = t["q"] + p.get("q_res", 0) + a
        if t.get("r") is not None:
            p["r"] = t["r"] + p.get("r_res", 0) + a
    for sq in out_sq:
        out_sq[sq]["adatt"] = adatt.get(sq, 0)
    return {"squadre": dict(out_sq), "piloti": dict(out_pil), "gare": len(fatti)}


def aggiorna_con_weekend(val, misure, rd, prima_di):
    """Corregge le valutazioni con le sessioni già finite del weekend in corso: libere (poco), qualifica sprint,
    sprint e qualifica (molto: stessa pista, stesse condizioni). Restituisce anche cosa è stato usato."""
    pil = {s: dict(p) for s, p in val["piloti"].items()}
    usato = []

    def miscela(tab, chi, peso, etichetta, sposta=0.0):
        if not tab:
            return
        c = mediana([pil[s].get(chi) for s in tab if s in pil and pil[s].get(chi) is not None]) or 0
        cm = mediana(list(tab.values()))
        for s, d in tab.items():
            if s in pil and pil[s].get(chi) is not None:
                pil[s][chi] = (1 - peso) * pil[s][chi] + peso * (d - cm + c) + sposta
        usato.append(etichetta)

    nomi_fp = sorted(n for n in misure.wk[rd]["sessioni"] if n.startswith("Practice"))
    for i, nome in enumerate(nomi_fp):
        if not misure.finita(rd, nome, prima_di):
            continue
        if i < len(misure.fp1.get(rd, [])):
            miscela(misure.fp1[rd][i], "q", 0.12, NOMI_SESSIONE.get(nome, nome) + " (giro secco)")
        if i < len(misure.fpl.get(rd, [])):
            miscela(misure.fpl[rd][i], "r", 0.12, NOMI_SESSIONE.get(nome, nome) + " (passo lungo)")
    if misure.finita(rd, "Sprint Qualifying", prima_di):
        miscela(misure.sq.get(rd), "q", 0.45, "Qualifica sprint")
    if misure.finita(rd, "Sprint", prima_di):
        miscela(misure.sp.get(rd), "r", 0.35, "Sprint")
    if misure.finita(rd, "Qualifying", prima_di):
        miscela(misure.q.get(rd), "q", 0.5, "Qualifica")
    return pil, usato


# ======================================================================= circuiti

def storia_circuito(storia, ck, cittadino):
    """Safety car, rosse, pioggia, soste e peso della griglia sul circuito, dalle gare dal 2023 (e dal 2026)."""
    tutte = [g for g in (storia or {}).get("gare", []) if g["tipo"] == "Race"]
    qui = [g for g in tutte if g["circuito"] == ck]
    rho_tutte = [_spearman(g["griglia_arrivo"]) for g in tutte]
    rho_tutte = [x for x in rho_tutte if x is not None]
    rho_media = media(rho_tutte) or 0.6
    if not qui:   # circuito nuovo: media dei circuiti dello stesso tipo
        simili = [g for g in tutte if (g["nome"] in CITTADINI) == cittadino]
        base = simili or tutte
        return {"gare": 0, "nuovo": True, **_aggrega(base, rho_media)}
    return {"gare": len(qui), "nuovo": False, "anni": sorted({g["anno"] for g in qui}), **_aggrega(qui, rho_media)}


CITTADINI = {"Melbourne", "Monte Carlo", "Baku", "Singapore", "Jeddah", "Miami", "Las Vegas", "Madrid"}


def _spearman(coppie):
    if len(coppie) < 8:
        return None
    n = len(coppie)
    rg = {x: i for i, x in enumerate(sorted(range(n), key=lambda i: coppie[i][0]))}
    ra = {x: i for i, x in enumerate(sorted(range(n), key=lambda i: coppie[i][1]))}
    d2 = sum((rg[i] - ra[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


def _aggrega(gare, rho_media):
    n = len(gare) or 1
    soste = [s for g in gare for s in g["soste"]]
    seq = Counter(x for g in gare for x in g["sequenze"] if "?" not in x and "U" not in x)
    rho = media([_spearman(g["griglia_arrivo"]) for g in gare])
    vinte_pole = [1 if any(gr == 1 and pos == 1 for gr, pos in g["griglia_arrivo"]) else 0 for g in gare
                  if g["griglia_arrivo"]]
    vincitori_griglia = [gr for g in gare for gr, pos in g["griglia_arrivo"] if pos == 1]
    return {
        "p_sc": sum(1 for g in gare if g["sc"]) / n, "p_vsc": sum(1 for g in gare if g["vsc"]) / n,
        "p_rossa": sum(1 for g in gare if g["rosse"]) / n, "p_pioggia": sum(1 for g in gare if g["pioggia"]) / n,
        "sc_medie": sum(g["sc"] for g in gare) / n,
        "soste": dict(Counter(min(s, 3) for s in soste)), "soste_media": media(soste),
        "sequenze": seq.most_common(4), "rho": rho if rho is not None else rho_media, "rho_media": rho_media,
        "pole_vince": media(vinte_pole), "griglia_vincitore_max": max(vincitori_griglia) if vincitori_griglia else None,
        "elenco": [{"anno": g["anno"], "sc": g["sc"], "vsc": g["vsc"], "rosse": g["rosse"], "pioggia": g["pioggia"],
                    "soste": mediana(g["soste"])} for g in sorted(gare, key=lambda g: -g["anno"])][:8],
    }


def costo_griglia(circ, kg):
    """Secondi che vale una posizione in griglia: più la griglia decide l'arrivo, più è difficile sorpassare."""
    rho = min(max(circ["rho"], 0.2), 0.93)
    rm = min(max(circ["rho_media"], 0.2), 0.9)
    return kg * (rho / (1 - rho)) / (rm / (1 - rm))


# ======================================================================= simulazione

def prob_ritiro(st_ris, misure, prima_di_round):
    """Probabilità di ritiro per squadra: quante volte le sue macchine non hanno finito nel 2026, avvicinata alla
    media (con poche gare conta poco)."""
    per = defaultdict(lambda: [0, 0])
    tot = [0, 0]
    for g in st_ris:
        if int(g["round"]) >= prima_di_round:
            continue
        for x in g["Results"]:
            sig = x["Driver"].get("code")
            sq = misure.squadra.get((int(g["round"]), sig)) or x["Constructor"]["name"]
            fuori = not (x["status"] in ("Finished", "Lapped") or x["status"].startswith("+"))
            if x["status"] in ("Did not start", "Withdrew"):
                continue
            per[sq][0] += fuori
            per[sq][1] += 1
            tot[0] += fuori
            tot[1] += 1
    base = (tot[0] + RITIRO_BASE * 40) / (tot[1] + 40)
    return {sq: (a + base * 12) / (n + 12) for sq, (a, n) in per.items()}, base


def simula(pil, evento, circ, par, n_sim=6000, griglia=None, ritiro=None, ritiro_base=RITIRO_BASE, p_pioggia=0.0,
           seed=1):
    """Monte Carlo di qualifica (se la griglia non c'è ancora) e gara o sprint.
    pil: {sigla: {q, r, squadra}}; evento: "Race" o "Sprint"."""
    rng = random.Random(seed)
    piloti = [s for s, p in pil.items() if p.get("q") is not None or p.get("r") is not None]
    if len(piloti) < 10:
        return None
    qv = {s: pil[s].get("q") if pil[s].get("q") is not None else pil[s].get("r") for s in piloti}
    rv = {}
    for s in piloti:
        q, rr = pil[s].get("q"), pil[s].get("r")
        rv[s] = 0.65 * rr + 0.35 * q if rr is not None and q is not None else (rr if rr is not None else q)
    sprint = evento == "Sprint"
    durata = DURATA_SPRINT if sprint else DURATA_GARA
    cg = costo_griglia(circ, par["kg"]) * (0.8 if sprint else 1.0)
    p_sc = circ["p_sc"] * (0.45 if sprint else 1.0)
    ritiro = ritiro or {}
    k = len(piloti)
    conta = {s: {"vince": 0, "podio": 0, "punti": 0, "pos": 0.0, "ritiro": 0, "pole": 0, "q10": 0, "pt": 0.0}
             for s in piloti}
    tabella = PUNTI_SPRINT if sprint else PUNTI_GARA
    team = sorted(set(pil[s].get("squadra") for s in piloti), key=str)
    sw = par.get("sw", 0.0)
    for _ in range(n_sim):
        # forma della squadra in questo weekend: una macchina può trovarsi bene o male su una pista
        forma = {t: rng.gauss(0, sw) for t in team}
        if griglia:
            ordine_g = [s for s in griglia if s in conta] + [s for s in piloti if s not in griglia]
        else:
            tempi = {s: qv[s] + forma[pil[s].get("squadra")] + rng.gauss(0, par["sq"]) + (1.5 if rng.random() < 0.03 else 0)
                     for s in piloti}
            ordine_g = sorted(piloti, key=tempi.get)
            conta[ordine_g[0]]["pole"] += 1
            for s in ordine_g[:10]:
                conta[s]["q10"] += 1
        pioggia = rng.random() < p_pioggia
        sc = rng.random() < p_sc
        sr = par["sr"] * (2.3 if pioggia else 1.0)
        fine = {}
        fuori = []
        for i, s in enumerate(ordine_g):
            pr = ritiro.get(pil[s].get("squadra"), ritiro_base) * (0.4 if sprint else 1.0) + (0.04 if pioggia else 0)
            if rng.random() < pr:
                fuori.append(s)
                continue
            deficit = (rv[s] + forma[pil[s].get("squadra")] + rng.gauss(0, sr)) / 100 * durata
            if sc:
                deficit = deficit * 0.6 + rng.gauss(0, 4.0)
            fine[s] = deficit + i * cg * (0.8 if pioggia else 1.0) + rng.gauss(0, par["s1"])
        ordine = sorted(fine, key=fine.get) + fuori
        for pos, s in enumerate(ordine):
            c = conta[s]
            if s in fine:
                if pos == 0:
                    c["vince"] += 1
                if pos < 3:
                    c["podio"] += 1
                if pos < len(tabella):
                    c["punti"] += 1
                    c["pt"] += tabella[pos]
            else:
                c["ritiro"] += 1
            c["pos"] += pos + 1
    out = {}
    for s, c in conta.items():
        out[s] = {"vince": c["vince"] / n_sim, "podio": c["podio"] / n_sim, "punti": c["punti"] / n_sim,
                  "pos": c["pos"] / n_sim, "ritiro": c["ritiro"] / n_sim, "pt": c["pt"] / n_sim,
                  "squadra": pil[s].get("squadra"), "q": pil[s].get("q"), "r": rv[s]}
        if not griglia:
            out[s]["pole"] = c["pole"] / n_sim
            out[s]["q10"] = c["q10"] / n_sim
    return out


def simula_titolo(st, wk, val_fn, storia, par, ritiro, ritiro_base, n_sim=2500, seed=7):
    """Il resto della stagione simulato gara per gara: probabilità di titolo piloti e costruttori."""
    cp = (st.get("classifica_piloti") or {}).get("DriverStandings") or []
    cc = (st.get("classifica_costruttori") or {}).get("ConstructorStandings") or []
    if not cp:
        return None
    fatte = int(st["classifica_piloti"]["round"])
    restano = [w for w in wk if w["round"] > fatte]
    punti0 = {x["Driver"]["code"]: float(x["points"]) for x in cp}
    squadra_di = {}
    eventi = []
    for w in restano:
        circ = storia_circuito(storia, w["ck"], w["cittadino"])
        pil = val_fn(w)
        for s, p in pil.items():
            squadra_di[s] = p.get("squadra")
        if w["sprint"]:
            eventi.append(("Sprint", w, circ, pil))
        eventi.append(("Race", w, circ, pil))
    rng = random.Random(seed)
    titoli = Counter()
    titoli_c = Counter()
    finali = defaultdict(float)
    finali_c = defaultdict(float)
    sq_punti0 = defaultdict(float)
    for s, p in punti0.items():
        sq_punti0[squadra_di.get(s) or s] += p
    # i punti dei costruttori veri (un pilota può aver cambiato squadra): si parte da quelli di Jolpica, con i nomi
    # di OpenF1 dove si riesce
    nomi_c = {}
    for x in cp:
        nomi_c[x["Constructors"][-1]["constructorId"]] = squadra_di.get(x["Driver"]["code"])
    c0 = {nomi_c.get(x["Constructor"]["constructorId"]) or x["Constructor"]["name"]: float(x["points"]) for x in cc}
    for i in range(n_sim):
        pt = dict(punti0)
        ptc = dict(c0)
        # le squadre sviluppano la macchina: un piccolo spostamento casuale a ogni gara
        # anche i piloti hanno periodi migliori e peggiori
        deriva = defaultdict(float)
        deriva_p = defaultdict(float)
        for tipo, w, circ, pil in eventi:
            if tipo == "Race" or not w["sprint"]:
                for sq in set(p.get("squadra") for p in pil.values()):
                    deriva[sq] += rng.gauss(0, 0.06)
                for s in pil:
                    deriva_p[s] += rng.gauss(0, 0.05)
            d = {s: deriva[p.get("squadra")] + deriva_p[s] for s, p in pil.items()}
            pil2 = {s: dict(p, q=(p["q"] + d[s]) if p.get("q") is not None else None,
                            r=(p["r"] + d[s]) if p.get("r") is not None else None)
                    for s, p in pil.items()}
            ris = simula(pil2, tipo, circ, par, n_sim=1, ritiro=ritiro, ritiro_base=ritiro_base,
                         seed=rng.randrange(1 << 30))
            if not ris:
                continue
            ordine = sorted(ris, key=lambda s: ris[s]["pos"])
            tab = PUNTI_SPRINT if tipo == "Sprint" else PUNTI_GARA
            for pos, s in enumerate(ordine[:len(tab)]):
                if ris[s]["ritiro"]:
                    continue
                pt[s] = pt.get(s, 0) + tab[pos]
                sq = ris[s]["squadra"]
                ptc[sq] = ptc.get(sq, 0) + tab[pos]
        top = max(pt.values())
        vincitori = [s for s, p in pt.items() if p == top]
        titoli[rng.choice(vincitori)] += 1
        topc = max(ptc.values())
        titoli_c[rng.choice([s for s, p in ptc.items() if p == topc])] += 1
        for s, p in pt.items():
            finali[s] += p
        for s, p in ptc.items():
            finali_c[s] += p
    return {
        "gare_rimaste": sum(1 for e in eventi if e[0] == "Race"), "sprint_rimaste": sum(1 for e in eventi if e[0] == "Sprint"),
        "punti_in_palio": sum(26 if e[0] == "Race" else 8 for e in eventi) - sum(1 for e in eventi if e[0] == "Race"),
        "piloti": sorted([{"sigla": s, "punti": punti0.get(s, 0), "titolo": titoli[s] / n_sim, "finali": finali[s] / n_sim,
                           "squadra": squadra_di.get(s)} for s in punti0], key=lambda x: -x["finali"]),
        "costruttori": sorted([{"squadra": s, "punti": c0.get(s, 0), "titolo": titoli_c[s] / n_sim,
                                "finali": finali_c[s] / n_sim} for s in c0], key=lambda x: -x["finali"]),
    }


# ======================================================================= taratura e storico

def punteggio(prev, ordine, fuori):
    """Quanto un pronostico ha visto giusto: log-verosimiglianza media su vincitore, podio e punti."""
    eps = 1e-3
    tot, n = 0.0, 0
    if not ordine or ordine[0] in fuori:
        return None
    vinc = ordine[0]
    tot += math.log(max(prev.get(vinc, {}).get("vince", 0), eps))
    n += 1
    podio = set(o for o in ordine[:3] if o not in fuori)
    punti = set(o for o in ordine[:10] if o not in fuori)
    for s, p in prev.items():
        for chi, insieme in (("podio", podio), ("punti", punti)):
            q = min(max(p[chi], eps), 1 - eps)
            tot += math.log(q if s in insieme else 1 - q) / 6
    return tot


def griglia_da(misure, rd, sess, evento):
    """La griglia di partenza dall'ordine della qualifica (o della qualifica sprint)."""
    nome = "Sprint Qualifying" if evento == "Sprint" else "Qualifying"
    sk = misure.wk[rd]["sessioni"].get(nome)
    s = sess.get(sk)
    if not s:
        return None
    ordine, _ = risultato_ordinato(s)
    return ordine or None


def previsione_evento(contesto, w, evento, prima_di, par, n_sim=6000, seed=1):
    """Pronostico di una sprint o di una gara con i dati disponibili prima di `prima_di`."""
    misure, sess, storia, st = contesto["misure"], contesto["sess"], contesto["storia"], contesto["st"]
    rd = w["round"]
    val = valuta(misure, prima_di, w["cittadino"], solo_round_prima=rd)
    pil = val["piloti"]
    usato = []
    if any(misure.finita(rd, n, prima_di) for n in w["sessioni"]):
        pil, usato = aggiorna_con_weekend({"piloti": pil}, misure, rd, prima_di)
    # solo chi corre questo weekend, se lo sappiamo (le libere 1 no: spesso ci sono i giovani al posto dei titolari)
    iscritti = set()
    for nome, sk in w["sessioni"].items():
        if nome != "Practice 1" and sk in sess and sess[sk]["inizio"] < prima_di:
            iscritti |= set(squadre(sess[sk]))
    if len(iscritti) >= 18:
        pil = {s: p for s, p in pil.items() if s in iscritti}
    nome_q = "Sprint Qualifying" if evento == "Sprint" else "Qualifying"
    griglia = griglia_da(misure, rd, sess, evento) if misure.finita(rd, nome_q, prima_di) else None
    circ = storia_circuito(storia, w["ck"], w["cittadino"])
    ritiro, base = prob_ritiro(st.get("risultati") or [], misure, rd)
    pp = contesto.get("pioggia", {}).get((rd, evento), 0.0)
    ris = simula(pil, evento, circ, par, n_sim=n_sim, griglia=griglia, ritiro=ritiro, ritiro_base=base,
                 p_pioggia=pp, seed=seed)
    return {"prev": ris, "griglia": griglia, "usato": usato, "circ": circ, "val": val, "pioggia": pp,
            "ritiro": ritiro, "ritiro_base": base}


def eventi_finiti(contesto, prima_di=None):
    """Sprint e gare 2026 già corse, con l'ordine d'arrivo, dal terzo weekend (prima i dati sono troppo pochi)."""
    out = []
    for w in contesto["wk"]:
        for ev in ("Sprint", "Race"):
            sk = w["sessioni"].get(ev)
            s = contesto["sess"].get(sk)
            if not s or not s.get("risultato") or (prima_di and s["fine"] >= prima_di):
                continue
            ordine, fuori = risultato_ordinato(s)
            if ordine and w["round"] >= 3:
                out.append((w, ev, s, ordine, fuori))
    return out


def tara(contesto, n_sim=1200, partenza=None):
    """Sceglie le incertezze del modello che avrebbero previsto meglio le sprint e le gare 2026 già corse (con la
    griglia vera). Ricerca a passi: parte dai valori dell'ultima taratura e prova a ingrandirli e rimpicciolirli
    uno alla volta, finché non migliora più."""
    eventi = eventi_finiti(contesto)
    if len(eventi) < 5:
        return dict(PARAMETRI), None

    def voto(par):
        tot = 0.0
        for w, ev, s, ordine, fuori in eventi:
            p = previsione_evento(contesto, w, ev, s["inizio"], par, n_sim=n_sim)
            sc = punteggio(p["prev"] or {}, ordine, fuori) if p["prev"] else None
            tot += sc if sc is not None else 0
        return tot

    par = dict(PARAMETRI, **(partenza or {}))
    best = voto(par)
    limiti = {"sr": (0.1, 2.0), "kg": (0.5, 30.0), "sw": (0.0, 0.8), "s1": (0.5, 15.0)}
    for passo in (1.6, 1.25):
        migliorato = True
        giri = 0
        while migliorato and giri < 4:
            migliorato, giri = False, giri + 1
            for k, (lo, hi) in limiti.items():
                for f in (passo, 1 / passo):
                    v = min(max(par[k] * f if par[k] else 0.05, lo), hi)
                    if v == par[k]:
                        continue
                    prova = dict(par, **{k: v})
                    sc = voto(prova)
                    if sc > best + 0.05:
                        best, par, migliorato = sc, prova, True
                        break
    # incertezza della qualifica: sulle pole vere
    gare = []
    for w, ev, s, ordine, fuori in eventi:
        if ev != "Race":
            continue
        gq = griglia_da(contesto["misure"], w["round"], contesto["sess"], "Race")
        sk = w["sessioni"].get("Qualifying")
        if gq and sk in contesto["sess"]:
            gare.append((w, contesto["sess"][sk]["inizio"], gq[0]))

    def voto_q(sq):
        tot = 0.0
        for w, inizio_q, pole in gare:
            p = previsione_evento(contesto, w, "Race", inizio_q, dict(par, sq=sq), n_sim=n_sim)
            if p["prev"] and pole in p["prev"]:
                tot += math.log(max(p["prev"][pole].get("pole", 0), 1e-3))
        return tot

    sq, qbest = par["sq"], voto_q(par["sq"])
    for f in (1.6, 1 / 1.6, 1.25, 1 / 1.25):
        v = min(max(sq * f, 0.04), 1.0)
        sc = voto_q(v)
        if sc > qbest + 0.05:
            sq, qbest = v, sc
    par["sq"] = sq
    return {k: round(v, 3) for k, v in par.items()}, {"eventi": len(eventi), "punteggio": round(best, 2)}


def voce_storico(w, ev, prev, ordine=None, fuori=None, ricostruita=False, at=None, griglia=None):
    v = {"round": w["round"], "gp": w["nome"], "evento": ev, "at": at or time.time(), "ricostruita": ricostruita,
         "griglia": griglia,
         "prev": {s: [r(p["vince"]), r(p["podio"]), r(p["punti"]), r(p["pos"], 2), r(p["ritiro"])] for s, p in prev.items()}}
    if ordine:
        v["ordine"], v["fuori"] = ordine, sorted(fuori or [])
    return v


def riepilogo_storico(storico):
    """Quante volte il favorito ha vinto, e «quanto fidarsi»: per fascia di probabilità del podio, quante volte
    è successo davvero."""
    finite = [v for v in storico if v.get("ordine")]
    fasce = [(0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]
    conta = {f: [0, 0, 0.0] for f in fasce}
    fav_vince, fav_podio, brier, n_brier = 0, 0, 0.0, 0
    for v in finite:
        prev = v["prev"]
        fav = max(prev, key=lambda s: prev[s][0])
        if v["ordine"][0] == fav and fav not in v["fuori"]:
            fav_vince += 1
        if fav in v["ordine"][:3] and fav not in v["fuori"]:
            fav_podio += 1
        podio = set(o for o in v["ordine"][:3] if o not in v["fuori"])
        for s, p in prev.items():
            for f in fasce:
                if f[0] <= p[1] < f[1]:
                    conta[f][0] += 1
                    conta[f][1] += s in podio
                    conta[f][2] += p[1]
            brier += (p[0] - (1 if s == v["ordine"][0] else 0)) ** 2
            n_brier += 1
    return {"eventi": len(finite), "favorito_vince": fav_vince, "favorito_podio": fav_podio,
            "fasce": [{"da": f[0], "a": min(f[1], 1), "casi": c[0], "successi": c[1],
                       "attesa": c[2] / c[0] if c[0] else None} for f, c in conta.items()],
            "brier_vittoria": brier / n_brier if n_brier else None}


# ======================================================================= statistiche di stagione

def statistiche(contesto, val):
    st, misure, sess, wk = contesto["st"], contesto["misure"], contesto["sess"], contesto["wk"]
    piloti = defaultdict(lambda: defaultdict(float))
    info = {}
    for w in wk:
        for nome, sk in w["sessioni"].items():
            s = sess.get(sk)
            if s:
                for p in s["piloti"].values():
                    if p.get("sigla"):
                        info[p["sigla"]] = {"nome": p["nome"], "squadra": p["squadra"], "colore": p["colore"],
                                            "foto": p["foto"], "nome_breve": p["nome_breve"]}
    gare_pil = defaultdict(list)
    for g in st.get("risultati") or []:
        rd = int(g["round"])
        for x in g["Results"]:
            sig = x["Driver"]["code"]
            p = piloti[sig]
            fin = x["status"] in ("Finished", "Lapped") or x["status"].startswith("+")
            pos = int(x["position"])
            grid = int(x["grid"]) or 22
            p["gare"] += 1
            p["punti_gara"] += float(x["points"])
            p["vittorie"] += pos == 1
            p["podi"] += pos <= 3
            p["top10"] += pos <= 10
            p["ritiri"] += not fin
            if fin:
                p["somma_arrivo"] += pos
                p["arrivi"] += 1
                p["guadagnate"] += grid - pos
            p["somma_griglia"] += grid
            gare_pil[sig].append({"round": rd, "gp": nome_gp(g["raceName"]), "griglia": int(x["grid"]),
                                  "pos": pos if fin else None, "stato": x["status"], "punti": float(x["points"])})
            info.setdefault(sig, {"nome": x["Driver"]["givenName"] + " " + x["Driver"]["familyName"],
                                  "squadra": x["Constructor"]["name"]})
            info[sig]["numero"] = x.get("number")
            info[sig]["nazionalita"] = x["Driver"].get("nationality")
    for g in st.get("sprint") or []:
        for x in g["SprintResults"]:
            piloti[x["Driver"]["code"]]["punti_sprint"] += float(x["points"])
            piloti[x["Driver"]["code"]]["vittorie_sprint"] += int(x["position"]) == 1
    for g in st.get("qualifiche") or []:
        for x in g["QualifyingResults"]:
            p = piloti[x["Driver"]["code"]]
            p["pole"] += int(x["position"]) == 1
            p["somma_qualifica"] += int(x["position"])
            p["qualifiche"] += 1
            p["q3"] += int(x["position"]) <= 10
    # testa a testa col compagno e distacchi medi, da OpenF1
    h2h = defaultdict(lambda: {"q": [0, 0], "r": [0, 0], "gap_q": []})
    velocita = defaultdict(list)
    sorpassi = defaultdict(lambda: [0, 0])
    giri_veloci = Counter()
    for w in wk:
        rd = w["round"]
        q = misure.q.get(rd) or {}
        sq_rd = {}
        for sig in q:
            sq_rd.setdefault(misure.squadra.get((rd, sig)), []).append(sig)
        for sq, ss in sq_rd.items():
            if len(ss) == 2:
                a, b = ss
                h2h[a]["q"][0 if q[a] < q[b] else 1] += 1
                h2h[b]["q"][0 if q[b] < q[a] else 1] += 1
                h2h[a]["gap_q"].append(q[a] - q[b])
                h2h[b]["gap_q"].append(q[b] - q[a])
        s = sess.get(w["sessioni"].get("Race"))
        if s:
            ordine, fuori = risultato_ordinato(s)
            per_sq = defaultdict(list)
            for i, sig in enumerate(ordine):
                if sig not in fuori:
                    per_sq[misure.squadra.get((rd, sig))].append((i, sig))
            for xs in per_sq.values():
                if len(xs) == 2:
                    (ia, a), (ib, b) = xs
                    h2h[a]["r"][0] += 1
                    h2h[b]["r"][1] += 1
            sg = sigle(s)
            best = {}
            vmax = defaultdict(int)
            for g in s.get("giri") or []:
                if g[0] in sg:
                    if g[7]:
                        vmax[sg[g[0]]] = max(vmax[sg[g[0]]], g[7])
                    if g[2] and not g[6]:
                        best[sg[g[0]]] = min(best.get(sg[g[0]], 1e9), g[2])
            if best:
                giri_veloci[min(best, key=best.get)] += 1
            if vmax:
                mv = mediana(vmax.values())
                for sig, v in vmax.items():
                    velocita[sig].append(v - mv)
            for a, b, _ in s.get("sorpassi") or []:
                if a in sg:
                    sorpassi[sg[a]][0] += 1
                if b in sg:
                    sorpassi[sg[b]][1] += 1
    cp = {x["Driver"]["code"]: x for x in ((st.get("classifica_piloti") or {}).get("DriverStandings") or [])}
    out_p = []
    for sig, p in piloti.items():
        i = info.get(sig, {})
        v = val["piloti"].get(sig, {})
        c = cp.get(sig)
        out_p.append({
            "sigla": sig, "nome": i.get("nome"), "squadra": i.get("squadra"), "colore": i.get("colore"),
            "foto": i.get("foto"), "numero": i.get("numero"), "nazionalita": i.get("nazionalita"),
            "posizione": int(c["position"]) if c and c.get("position") else None,
            "punti": float(c["points"]) if c else p["punti_gara"] + p["punti_sprint"],
            "gare": int(p["gare"]), "vittorie": int(p["vittorie"]), "podi": int(p["podi"]), "top10": int(p["top10"]),
            "pole": int(p["pole"]), "q3": int(p["q3"]), "ritiri": int(p["ritiri"]),
            "vittorie_sprint": int(p["vittorie_sprint"]), "punti_sprint": p["punti_sprint"],
            "media_arrivo": r(p["somma_arrivo"] / p["arrivi"], 1) if p["arrivi"] else None,
            "media_griglia": r(p["somma_griglia"] / p["gare"], 1) if p["gare"] else None,
            "media_qualifica": r(p["somma_qualifica"] / p["qualifiche"], 1) if p["qualifiche"] else None,
            "guadagnate": r(p["guadagnate"] / p["arrivi"], 1) if p["arrivi"] else None,
            "h2h_q": h2h[sig]["q"], "h2h_r": h2h[sig]["r"], "gap_compagno": r(media(h2h[sig]["gap_q"])),
            "passo_q": r(v.get("q")), "passo_r": r(v.get("r")), "giri_veloci": giri_veloci[sig],
            "velocita": r(media(velocita[sig]), 1), "sorpassi": sorpassi[sig][0], "sorpassato": sorpassi[sig][1],
            "gare_elenco": gare_pil[sig],
        })
    out_p.sort(key=lambda x: (x["posizione"] or 99, -x["punti"]))
    # squadre
    cc = (st.get("classifica_costruttori") or {}).get("ConstructorStandings") or []
    squadra_jolpica = {}
    for g in st.get("risultati") or []:
        for x in g["Results"]:
            sq = misure.squadra.get((int(g["round"]), x["Driver"]["code"]))
            if sq:
                squadra_jolpica[x["Constructor"]["constructorId"]] = sq
    deg = defaultdict(lambda: defaultdict(list))
    rds = sorted(misure.deg)
    for i, rd in enumerate(rds):
        peso = RECENZA ** (len(rds) - 1 - i)
        for x in misure.deg[rd]:
            sq = misure.squadra.get((rd, x["sigla"]))
            deg[sq][x["mescola"]].append((x["degrado"], peso * min(x["giri"], 20)))
    pit = defaultdict(list)
    for w in wk:
        s = sess.get(w["sessioni"].get("Race"))
        if s:
            sg = sigle(s)
            sq = squadre(s)
            for n, lap, corsia, fermo in s.get("pit") or []:
                if fermo and 1.6 < fermo < 6 and n in sg:
                    pit[sq.get(sg[n])].append(fermo)
    tutte_deg = defaultdict(list)
    for sq, d in deg.items():
        for m, v in d.items():
            tutte_deg[m].extend(v)
    deg_media = {m: wmedia(v) for m, v in tutte_deg.items()}
    colori = {}
    for x in out_p:
        if x.get("colore"):
            colori.setdefault(x["squadra"], x["colore"])
    vsq = val["squadre"]
    out_s = []
    for x in cc:
        cid = x["Constructor"]["constructorId"]
        sq = squadra_jolpica.get(cid) or x["Constructor"]["name"]
        v = vsq.get(sq, {})
        membri = [p for p in out_p if p["squadra"] == sq]
        stor = andamento_squadra(misure, sq)
        out_s.append({
            "squadra": sq, "id": cid, "posizione": int(x["position"]) if x.get("position") else None,
            "punti": float(x["points"]), "vittorie": int(x["wins"]), "colore": colori.get(sq),
            "piloti": [p["sigla"] for p in membri], "podi": sum(p["podi"] for p in membri),
            "pole": sum(p["pole"] for p in membri), "ritiri": sum(p["ritiri"] for p in membri),
            "passo_q": r(v.get("q")), "passo_r": r(v.get("r")), "cittadini": r(cittadini_squadra(misure, sq)),
            "degrado": {m: r(wmedia(d), 3) for m, d in deg[sq].items() if len(d) >= 3},
            "pit": r(mediana(pit.get(sq, [])), 2), "pit_n": len(pit.get(sq, [])),
            "velocita": r(media([p["velocita"] for p in membri if p["velocita"] is not None]), 1),
            "andamento": stor,
        })
    return {"piloti": out_p, "squadre": out_s, "degrado_medio": {m: r(v, 3) for m, v in deg_media.items()},
            "pit_medio": r(mediana([x for v in pit.values() for x in v]), 2)}


def andamento_squadra(misure, sq):
    """Distacco in qualifica e in gara weekend per weekend (media dei due piloti)."""
    out = []
    for rd in sorted(misure.wk):
        q = [d for s, d in (misure.q.get(rd) or {}).items() if misure.squadra.get((rd, s)) == sq]
        g = [d for s, d in (misure.r.get(rd) or {}).items() if misure.squadra.get((rd, s)) == sq]
        if q or g:
            out.append([rd, r(media(q)), r(media(g))])
    return out


def cittadini_squadra(misure, sq):
    """Quanto la squadra va meglio (negativo) o peggio sui circuiti cittadini rispetto ai permanenti, in qualifica."""
    c, p = [], []
    for rd, tab in misure.q.items():
        xs = [d for s, d in tab.items() if misure.squadra.get((rd, s)) == sq]
        if xs:
            (c if misure.wk[rd]["cittadino"] else p).append(media(xs))
    if len(c) < 2 or len(p) < 2:
        return None
    return media(c) - media(p)


# ======================================================================= spiegazione

def perche(prev, val, circ, w, evento, usato, griglia, pioggia, statistiche_sq):
    """Le ragioni del pronostico, in frasi brevi."""
    out = []
    ordine = sorted(prev, key=lambda s: -prev[s]["vince"])
    fav = ordine[0]
    sq_fav = prev[fav]["squadra"]
    vs = val["squadre"]
    migl_r = sorted([(v["r"], s) for s, v in vs.items() if v.get("r") is not None])
    migl_q = sorted([(v["q"], s) for s, v in vs.items() if v.get("q") is not None])
    if migl_r:
        a = migl_r[0]
        if len(migl_r) > 1:
            b = migl_r[1]
            d = b[0] - a[0]
            out.append(f"Passo gara: {a[1]} è la squadra più veloce nelle ultime gare, {it(d)}% al giro meglio di {b[1]} "
                       f"(circa {it(d / 100 * 95)} s al giro).")
        else:
            out.append(f"Passo gara: {a[1]} è la squadra più veloce nelle ultime gare.")
    if migl_q and migl_q[0][1] != (migl_r[0][1] if migl_r else None):
        out.append(f"In qualifica il riferimento è invece {migl_q[0][1]}.")
    if griglia:
        out.append(f"Griglia già decisa: parte davanti {griglia[0]}"
                   + (f", poi {griglia[1]} e {griglia[2]}." if len(griglia) > 2 else "."))
    rho = circ["rho"]
    if rho > 0.75:
        out.append(f"Qui sorpassare è difficile (griglia e arrivo coincidono molto, indice {it(rho)}): conta la qualifica.")
    elif rho < 0.55:
        out.append(f"Pista dove si sorpassa (indice griglia-arrivo {it(rho)}): il passo gara pesa più della posizione di partenza.")
    if circ["p_sc"] >= 0.6:
        out.append(f"Safety car in {circ['p_sc'] * 100:.0f}% delle gare qui dal 2023: rimescola i distacchi e aiuta chi insegue.")
    elif circ["p_sc"] <= 0.3:
        out.append(f"Safety car rara qui ({circ['p_sc'] * 100:.0f}% delle gare): vince di solito chi è più veloce.")
    if circ.get("nuovo"):
        out.append("Circuito senza storia recente: per safety car e sorpassi si usa la media dei circuiti simili.")
    if pioggia >= 0.25:
        out.append(f"Pioggia possibile ({pioggia * 100:.0f}%): più incertezza e più ritiri.")
    a = val["squadre"].get(sq_fav, {}).get("adatt", 0)
    if abs(a) >= 0.05:
        tipo = "cittadini" if w["cittadino"] else "permanenti"
        out.append(f"{sq_fav} sui circuiti {tipo} va {'meglio' if a < 0 else 'peggio'} del suo solito "
                   f"({it(abs(a))}%).")
    if usato:
        out.append("Del weekend sono già entrati nel calcolo: " + ", ".join(usato) + ".")
    return out
