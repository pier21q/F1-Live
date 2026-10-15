"""Analisi di ogni round (gara, sprint, qualifiche), progressione del mondiale e record della stagione.

Tutto viene dai dati già scaricati: i giri di OpenF1 (con l'orario in cui ogni giro comincia), gomme, pit stop,
direzione gara, sorpassi; e i risultati di Jolpica (griglia, stato, punti).
"""
from collections import Counter, defaultdict

import model

PUNTI_NC = ("Did not start", "Withdrew")


def _per_pilota(sess):
    sg = model.sigle(sess)
    per = defaultdict(list)
    for g in sess.get("giri") or []:
        if g[0] in sg and g[1] is not None:
            per[sg[g[0]]].append(g)
    for v in per.values():
        v.sort(key=lambda g: g[1])
    return per


def _passaggi(giri):
    """Orario in cui il pilota chiude ogni giro: l'inizio del giro dopo, o inizio più durata per l'ultimo."""
    out = {}
    for i, g in enumerate(giri):
        fine = None
        if i + 1 < len(giri) and giri[i + 1][1] == g[1] + 1 and giri[i + 1][8]:
            fine = giri[i + 1][8]
        elif g[8] and g[2]:
            fine = g[8] + g[2]
        if fine:
            out[g[1]] = fine
    return out


def posizioni(sess, griglia):
    """Posizione e distacco dal primo alla fine di ogni giro. Al giro 0 la posizione di partenza."""
    per = _per_pilota(sess)
    passi = {s: _passaggi(v) for s, v in per.items()}
    ultimo = max([max(p) for p in passi.values() if p] or [0])
    serie = {s: {"pos": [None] * (ultimo + 1), "gap": [None] * (ultimo + 1)} for s in passi}
    for i, s in enumerate(griglia or []):
        if s in serie:
            serie[s]["pos"][0] = i + 1
    for lap in range(1, ultimo + 1):
        chi = sorted((t[lap], s) for s, t in passi.items() if lap in t)
        if not chi:
            continue
        t0 = chi[0][0]
        for k, (t, s) in enumerate(chi):
            serie[s]["pos"][lap] = k + 1
            serie[s]["gap"][lap] = round(t - t0, 1)
    return ultimo, serie


def neutralizzazioni(sess):
    """Periodi di safety car, virtual safety car e bandiera rossa, in giri."""
    out, aperti = [], {}
    for e in sorted(sess.get("rc") or [], key=lambda e: e[0] or 0):
        lap, msg = e[1], (e[4] or "").upper()
        if lap is None:
            continue
        if e[2] == "SafetyCar":
            tipo = "VSC" if ("VSC" in msg or "VIRTUAL" in msg) else "SC"
            if "DEPLOYED" in msg and tipo not in aperti:
                aperti[tipo] = lap
            elif ("ENDING" in msg or "IN THIS LAP" in msg) and tipo in aperti:
                out.append({"tipo": tipo, "da": aperti.pop(tipo), "a": lap})
        if e[3] == "RED" and "Rossa" not in aperti:
            aperti["Rossa"] = lap
        if e[2] == "SessionStatus" and "Rossa" in aperti and ("RESUMED" in msg or "STARTED" in msg):
            out.append({"tipo": "Rossa", "da": aperti.pop("Rossa"), "a": lap})
    for tipo, da in aperti.items():
        out.append({"tipo": tipo, "da": da, "a": da})
    return sorted(out, key=lambda x: x["da"])


def strategie(sess):
    sg = model.sigle(sess)
    out = defaultdict(list)
    for g in sorted(sess.get("gomme") or [], key=lambda g: (g[0], g[1])):
        n, _, a, b, comp, eta = g
        if n in sg and a is not None and b is not None:
            out[sg[n]].append([comp or "?", a, b, eta or 0])
    return dict(out)


def pit_stop(sess):
    sg = model.sigle(sess)
    return [[sg[p[0]], p[1], p[2], p[3]] for p in sess.get("pit") or [] if p[0] in sg]


def passo_medio(sess):
    """Tempo mediano dei giri puliti di ogni pilota e distacco dal migliore."""
    gp = model.giri_puliti(sess, 5 if sess["nome"] == "Sprint" else 8)
    tempi = {s: model.mediana([t for _, t, _ in v]) for s, v in gp.items()}
    if not tempi:
        return []
    best = min(tempi.values())
    return sorted([[s, round(t, 3), round(t - best, 3), len(gp[s])] for s, t in tempi.items()], key=lambda x: x[1])


def giro_veloce(per):
    best = None
    for s, gs in per.items():
        for g in gs:
            if g[2] and not g[6] and (best is None or g[2] < best[1]):
                best = (s, g[2], g[1])
    return list(best) if best else None


def velocita(per):
    out = {}
    for s, gs in per.items():
        v = [g[7] for g in gs if g[7]]
        if v:
            out[s] = max(v)
    return out


def sorpassi(sess):
    sg = model.sigle(sess)
    fatti, subiti = Counter(), Counter()
    for a, b, _ in sess.get("sorpassi") or []:
        if a in sg:
            fatti[sg[a]] += 1
        if b in sg:
            subiti[sg[b]] += 1
    return fatti, subiti


def analisi_corsa(sess, jres=None):
    """Gara o sprint: classifica, posizioni giro per giro, strategie, pit stop, passo, neutralizzazioni, sorpassi."""
    sg = model.sigle(sess)
    per = _per_pilota(sess)
    ris_of = {sg.get(x["n"]): x for x in sess.get("risultato") or [] if x["n"] in sg}
    righe = []
    if jres:
        for x in jres:
            sig = x["Driver"].get("code")
            o = ris_of.get(sig, {})
            fin = x["status"] in ("Finished", "Lapped") or x["status"].startswith("+")
            righe.append({"pos": int(x["position"]), "sigla": sig, "griglia": int(x["grid"]) or None,
                          "stato": None if x["status"] == "Finished" else x["status"], "arrivato": fin,
                          "punti": float(x["points"]), "giri": int(x["laps"]) if x.get("laps") else o.get("giri"),
                          "tempo": o.get("tempo") if o.get("pos") == 1 else None,
                          "distacco": o.get("distacco") if o.get("pos") and o.get("pos") > 1 else None})
    else:
        for x in sorted(ris_of.values(), key=lambda x: x.get("pos") or 99):
            righe.append({"pos": x.get("pos"), "sigla": sg[x["n"]], "griglia": None,
                          "stato": "ritirato" if x.get("dnf") else "squalificato" if x.get("dsq") else None,
                          "arrivato": not (x.get("dnf") or x.get("dsq") or x.get("dns")), "punti": x.get("punti") or 0,
                          "giri": x.get("giri"), "tempo": x.get("tempo") if x.get("pos") == 1 else None,
                          "distacco": x.get("distacco") if (x.get("pos") or 0) > 1 else None})
    griglia = [r["sigla"] for r in sorted([r for r in righe if r["griglia"]], key=lambda r: r["griglia"])]
    ultimo, serie = posizioni(sess, griglia)
    fatti, subiti = sorpassi(sess)
    vel = velocita(per)
    strat = strategie(sess)
    pits = pit_stop(sess)
    in_testa = Counter()
    for s, v in serie.items():
        in_testa[s] = sum(1 for p in v["pos"][1:] if p == 1)
    for r in righe:
        s = r["sigla"]
        r["soste"] = max(0, len(strat.get(s, [])) - 1)
        r["sorpassi"] = fatti.get(s, 0)
        r["in_testa"] = in_testa.get(s, 0)
        r["velocita"] = vel.get(s)
    return {
        "nome": model.NOMI_SESSIONE.get(sess["nome"], sess["nome"]), "giri": ultimo, "righe": righe,
        "posizioni": {s: v["pos"] for s, v in serie.items()}, "distacchi": {s: v["gap"] for s, v in serie.items()},
        "strategie": strat, "pit": pits, "passo": passo_medio(sess), "neutralizzazioni": neutralizzazioni(sess),
        "giro_veloce": giro_veloce(per), "sorpassi_totali": sum(fatti.values()),
        "penalita": [e[4] for e in sess.get("rc") or [] if "PENALTY" in (e[4] or "") and "SERVED" not in (e[4] or "")][-12:],
        "meteo": sess.get("meteo"),
    }


def analisi_qualifica(sess):
    """Tempi di Q1, Q2 e Q3, settori migliori e giro ideale (somma dei tre settori migliori)."""
    sg = model.sigle(sess)
    per = _per_pilota(sess)
    settori = {}
    for s, gs in per.items():
        b = [min((g[3 + i] for g in gs if g[3 + i] and not g[6]), default=None) for i in range(3)]
        if all(b):
            settori[s] = [round(x, 3) for x in b]
    migl = [min((v[i] for v in settori.values()), default=None) for i in range(3)]
    righe = []
    for x in sorted(sess.get("risultato") or [], key=lambda x: x.get("pos") or 99):
        s = sg.get(x["n"])
        if not s:
            continue
        t = x.get("tempo")
        fasi = [v for v in (t if isinstance(t, list) else [t])] if t is not None else []
        validi = [v for v in fasi if v]
        best = min(validi) if validi else None
        ideale = round(sum(settori[s]), 3) if s in settori else None
        righe.append({"pos": x.get("pos"), "sigla": s, "fasi": fasi, "best": best, "settori": settori.get(s),
                      "ideale": ideale, "giri": x.get("giri")})
    pole = min([r["best"] for r in righe if r["best"]] or [None]) if righe else None
    for r in righe:
        r["distacco"] = round(r["best"] - pole, 3) if r["best"] and pole else None
    return {"nome": model.NOMI_SESSIONE.get(sess["nome"], sess["nome"]), "righe": righe,
            "settori_migliori": migl, "ideale_migliore": round(sum(migl), 3) if all(migl) else None}


def analisi_round(w, sess, st):
    """Tutto quello che c'è da vedere su un round già corso (anche solo in parte)."""
    rd = w["round"]
    jr = next((g["Results"] for g in st.get("risultati") or [] if int(g["round"]) == rd), None)
    js = next((g["SprintResults"] for g in st.get("sprint") or [] if int(g["round"]) == rd), None)
    out = {"round": rd, "nome": w["nome"], "gp": w["gp"], "circuito": w["circuito"], "orari": w["orari"],
           "sprint": w["sprint"], "cittadino": w["cittadino"]}
    for chiave, nome, j in (("gara", "Race", jr), ("sprint_gara", "Sprint", js)):
        s = sess.get(w["sessioni"].get(nome))
        if s and s.get("risultato"):
            out[chiave] = analisi_corsa(s, j)
    for chiave, nome in (("qualifica", "Qualifying"), ("qualifica_sprint", "Sprint Qualifying")):
        s = sess.get(w["sessioni"].get(nome))
        if s and s.get("risultato"):
            out[chiave] = analisi_qualifica(s)
    return out if any(k in out for k in ("gara", "sprint_gara", "qualifica", "qualifica_sprint")) else None


def riepilogo(a):
    """Il riassunto di un round per la scheda Weekend: podio, pole, giro veloce, numeri chiave."""
    g = a.get("gara")
    if not g:
        return None
    arr = [r for r in g["righe"] if r["arrivato"]]
    q = a.get("qualifica")
    neutr = Counter(n["tipo"] for n in g["neutralizzazioni"])
    rimonta = max((r for r in arr if r["griglia"]), key=lambda r: r["griglia"] - r["pos"], default=None)
    return {"round": a["round"], "nome": a["nome"], "gp": a["gp"], "data": a["orari"].get("Gara"),
            "podio": [[r["sigla"], r["griglia"], r.get("tempo") if r["pos"] == 1 else r.get("distacco")] for r in arr[:3]],
            "pole": q["righe"][0]["sigla"] if q and q["righe"] else None,
            "giro_veloce": g["giro_veloce"], "sorpassi": g["sorpassi_totali"],
            "ritirati": sum(1 for r in g["righe"] if not r["arrivato"]),
            "sc": neutr.get("SC", 0), "vsc": neutr.get("VSC", 0), "rosse": neutr.get("Rossa", 0),
            "rimonta": [rimonta["sigla"], rimonta["griglia"], rimonta["pos"]] if rimonta and rimonta["griglia"] - rimonta["pos"] > 0 else None,
            "piu_giri_in_testa": max(((r["sigla"], r["in_testa"]) for r in g["righe"]), key=lambda x: x[1], default=None)}


def progressione(st, squadra_di):
    """Punti di piloti e squadre dopo ogni round (gara più sprint), per il grafico del mondiale."""
    per_round = defaultdict(lambda: defaultdict(float))
    per_round_sq = defaultdict(lambda: defaultdict(float))
    rounds = set()
    for tab, chiave in ((st.get("risultati") or [], "Results"), (st.get("sprint") or [], "SprintResults")):
        for g in tab:
            rd = int(g["round"])
            rounds.add(rd)
            for x in g[chiave]:
                sig = x["Driver"].get("code")
                pt = float(x["points"])
                per_round[rd][sig] += pt
                sq = squadra_di.get(x["Constructor"]["constructorId"]) or x["Constructor"]["name"]
                per_round_sq[rd][sq] += pt
    rounds = sorted(rounds)
    piloti = sorted({s for r in per_round.values() for s in r})
    sq = sorted({s for r in per_round_sq.values() for s in r})

    def cumula(tab, chi):
        out = {}
        for s in chi:
            tot, v = 0.0, []
            for rd in rounds:
                tot += tab[rd].get(s, 0)
                v.append(round(tot, 1))
            out[s] = v
        return out
    return {"round": rounds, "piloti": cumula(per_round, piloti), "squadre": cumula(per_round_sq, sq)}


def duelli(misure):
    """Per ogni pilota, gara per gara, il distacco in qualifica dal compagno (in %; negativo = più veloce)."""
    out = defaultdict(list)
    for rd in sorted(misure.q):
        q = misure.q[rd]
        per_sq = defaultdict(list)
        for s, d in q.items():
            per_sq[misure.squadra.get((rd, s))].append((s, d))
        for xs in per_sq.values():
            if len(xs) == 2:
                (a, da), (b, db) = xs
                out[a].append([rd, round(da - db, 3)])
                out[b].append([rd, round(db - da, 3)])
    return dict(out)


def record(analisi, squadra_di_pilota):
    """I record della stagione, dalle analisi dei round."""
    pit, vmax, rimonte, sorp, testa, vicini, pole_nette, giri_v = [], [], [], [], Counter(), [], [], []
    vittorie_pole = []
    for a in analisi:
        rd, gp = a["round"], a["nome"]
        g = a.get("gara")
        q = a.get("qualifica")
        if g:
            for s, lap, corsia, fermo in g["pit"]:
                if fermo and 1.5 < fermo < 10:
                    pit.append([fermo, s, rd, gp, lap])
            for r in g["righe"]:
                if r.get("velocita"):
                    vmax.append([r["velocita"], r["sigla"], rd, gp])
                if r["arrivato"] and r["griglia"] and r["griglia"] - r["pos"] > 0:
                    rimonte.append([r["griglia"] - r["pos"], r["sigla"], rd, gp, r["griglia"], r["pos"]])
                if r["sorpassi"]:
                    sorp.append([r["sorpassi"], r["sigla"], rd, gp])
                testa[r["sigla"]] += r["in_testa"]
            arr = [r for r in g["righe"] if r["arrivato"]]
            if len(arr) > 1 and isinstance(arr[1].get("distacco"), (int, float)):
                vicini.append([arr[1]["distacco"], arr[0]["sigla"], arr[1]["sigla"], rd, gp])
            if g["giro_veloce"]:
                s, t, lap = g["giro_veloce"]
                giri_v.append([rd, gp, s, t, lap])
            if q and q["righe"] and arr:
                vittorie_pole.append([rd, gp, q["righe"][0]["sigla"], arr[0]["sigla"], arr[0]["griglia"]])
        if q and len(q["righe"]) > 1 and q["righe"][1].get("distacco"):
            pole_nette.append([q["righe"][1]["distacco"], q["righe"][0]["sigla"], q["righe"][1]["sigla"], rd, gp])
    vinte_pole = sum(1 for v in vittorie_pole if v[2] == v[3])
    griglia_vincenti = Counter(v[4] for v in vittorie_pole if v[4])
    return {
        "pit": sorted(pit)[:10],
        "velocita": sorted(vmax, key=lambda x: -x[0])[:10],
        "rimonte": sorted(rimonte, key=lambda x: -x[0])[:10],
        "sorpassi": sorted(sorp, key=lambda x: -x[0])[:10],
        "in_testa": [[s, n, squadra_di_pilota.get(s)] for s, n in testa.most_common(10) if n],
        "vicini": sorted(vicini)[:6],
        "pole_nette": sorted(pole_nette, key=lambda x: -x[0])[:6],
        "giri_veloci": giri_v,
        "pole_vittoria": {"gare": len(vittorie_pole), "vinte_dalla_pole": vinte_pole,
                          "griglia_vincitori": sorted(griglia_vincenti.items())},
    }
