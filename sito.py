"""Sito di F1 Live su GitHub Pages.

GitHub Pages fa solo da vetrina: i dati li scarica e li calcola l'app (server.py --cloud) sui computer di GitHub
(GitHub Actions), anche con il Mac spento. Il repository ha quattro rami:
- main: il codice dell'app e la programmazione (.github/workflows/aggiorna.yml);
- dati: i dati da cui riparte ogni aggiornamento (un solo commit, sovrascritto ogni volta);
- sito: la pagina e la fotografia dei dati che GitHub Pages mostra (un solo commit, sovrascritto ogni volta);
- storico: una copia al giorno dello storico dei pronostici, con tutte le versioni.

    python sito.py codice   carica nel ramo main il codice dell'app (da rifare dopo ogni modifica)
    python sito.py dati     carica nel ramo "dati" i dati del Mac, al posto di quelli su GitHub
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICONA = ROOT / "icona.png"
CODICE = ["server.py", "model.py", "dati.py", "fonti.py", "sito.py", "pagina.html", "icona.png", "favicon.png",
          "requirements.txt"]
CLOUD_FILES = {"cloud/aggiorna.yml": ".github/workflows/aggiorna.yml", "cloud/README.md": "README.md"}
DATI = ["storico.json", "extra.json", "cache/stagione-*.json", "cache/circuiti.json", "cache/meteo.json",
        "cache/parametri.json", "cache/tracciati/*.json", "cache/sessioni/*.json"]
DATI_IGNORA = "*.tmp\nfotografia.json\nsito/\nvenv/\nsito.json\n"
NOME_GIT = "F1 Live"
EMAIL_GIT = "f1-live@users.noreply.github.com"

MANIFEST = {"name": "F1 Live", "short_name": "F1 Live", "start_url": "./", "scope": "./", "display": "standalone",
            "background_color": "#F4F3F1", "theme_color": "#C8102E",
            "icons": [{"src": "icona-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "icona-512.png", "sizes": "512x512", "type": "image/png"}]}


def riduci(src, dst, size):
    """Icona ridotta con sips (Mac). Su GitHub sips non c'è: si copia l'originale."""
    if shutil.which("sips"):
        subprocess.run(["sips", "-z", str(size), str(size), str(src), "--out", str(dst)], capture_output=True)
    else:
        shutil.copyfile(src, dst)


def prepara(site, foto):
    """Scrive nella cartella del sito la pagina, i dati, le icone e il manifest (per la schermata Home)."""
    site.mkdir(parents=True, exist_ok=True)
    pagina = (ROOT / "pagina.html").read_text()
    (site / "index.html").write_text(pagina)
    (site / "dati.json").write_text(json.dumps(foto, ensure_ascii=False, separators=(",", ":")))
    (site / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=1))
    (site / ".nojekyll").write_text("")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    if (ROOT / "favicon.png").exists():
        shutil.copyfile(ROOT / "favicon.png", site / "favicon.png")
    for size in (180, 192, 512):
        out = site / f"icona-{size}.png"
        if not out.exists() and ICONA.exists():
            riduci(ICONA, out, size)


def accesso():
    """Sul Mac l'accesso a GitHub è quello del programma gh (il token nel Portachiavi è quello di Serie A Live e
    non può scrivere in questo repository). Vale solo per i comandi di F1 Live: la configurazione di git non cambia.
    Su GitHub Actions l'accesso lo dà già il workflow."""
    gh = shutil.which("gh") or str(Path.home() / ".local" / "bin" / "gh")
    if os.environ.get("GITHUB_ACTIONS") or not Path(gh).exists():
        return []
    return ["-c", "credential.helper=", "-c", f"credential.helper=!'{gh}' auth git-credential"]


def git(cartella, *args, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")   # senza credenziali salvate fallisce invece di restare in attesa
    r = subprocess.run(["git", *accesso(), "-C", str(cartella), *args], capture_output=True, text=True, timeout=timeout,
                       env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


def collega(site, remote):
    """Crea il repository locale (una volta) e lo collega a quello su GitHub."""
    site.mkdir(parents=True, exist_ok=True)
    if not (site / ".git").exists():
        git(site, "init", "-q", "-b", "sito")
        git(site, "config", "user.name", NOME_GIT)
        git(site, "config", "user.email", EMAIL_GIT)
    code, url = git(site, "remote", "get-url", "origin")
    if code != 0:
        git(site, "remote", "add", "origin", remote)
    elif url != remote:
        git(site, "remote", "set-url", "origin", remote)


def salva(cartella):
    """Un solo commit con lo stato attuale."""
    git(cartella, "add", "-A")
    if git(cartella, "rev-parse", "--verify", "-q", "HEAD")[0] == 0:
        return git(cartella, "commit", "-q", "--amend", "--no-edit", "--allow-empty")
    return git(cartella, "commit", "-q", "-m", NOME_GIT)


def pubblica(cartella, rami=("sito",), lease=False):
    """Manda il commit ai rami indicati. Con lease non sovrascrive un ramo cambiato da altri nel frattempo."""
    salva(cartella)
    code, out = git(cartella, "push", "-q", "--force-with-lease" if lease else "-f", "origin", *[f"HEAD:{b}" for b in rami])
    return code == 0, out


def copia_storico(repo, src, msg, ramo="storico", nome="storico.json"):
    """Aggiunge una versione di src al ramo `ramo`, che al contrario di "dati" tiene tutte le versioni."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    run = lambda *a, inp=None: subprocess.run(["git", *accesso(), "-C", str(repo), *a], input=inp, capture_output=True,
                                             text=True, timeout=90, env=env)
    padre = run("rev-parse", "FETCH_HEAD").stdout.strip() if run("fetch", "-q", "--depth=1", "origin", ramo).returncode == 0 else None
    blob = run("hash-object", "-w", str(src)).stdout.strip()
    if padre and run("rev-parse", f"{padre}:{nome}").stdout.strip() == blob:
        return True, "Storico già salvato"
    albero = run("mktree", inp=f"100644 blob {blob}\t{nome}\n").stdout.strip()
    commit = run("commit-tree", albero, *(["-p", padre] if padre else []), "-m", msg).stdout.strip()
    if not commit:
        return False, "Copia dello storico non creata"
    r = run("push", "-q", "origin", f"{commit}:refs/heads/{ramo}")
    return r.returncode == 0, "Copia dello storico salvata" if r.returncode == 0 else (r.stdout + r.stderr).strip()


def pubblica_codice(remote):
    """Copia il codice dell'app nel ramo main del repository, da dove lo prende GitHub Actions."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "codice"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", *accesso(), "clone", "-q", "--depth", "1", "-b", "main", remote, str(dest)],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:   # repository vuoto: si parte da zero
            dest.mkdir(parents=True, exist_ok=True)
            git(dest, "init", "-q", "-b", "main")
            git(dest, "remote", "add", "origin", remote)
        git(dest, "config", "user.name", NOME_GIT)
        git(dest, "config", "user.email", EMAIL_GIT)
        git(dest, "rm", "-rq", "--ignore-unmatch", ".")
        for nome in CODICE:
            if (ROOT / nome).exists():
                shutil.copyfile(ROOT / nome, dest / nome)
        for src, nome in CLOUD_FILES.items():
            (dest / nome).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / src, dest / nome)
        git(dest, "add", "-A")
        if git(dest, "rev-parse", "--verify", "-q", "HEAD")[0] == 0 and git(dest, "diff", "--cached", "--quiet")[0] == 0:
            return True, "Il codice su GitHub è già aggiornato"
        git(dest, "commit", "-q", "-m", "Aggiorna il codice di F1 Live")
        code, out = git(dest, "push", "-q", "origin", "HEAD:main")
        return code == 0, "Codice caricato su GitHub" if code == 0 else out


def pubblica_dati(data_dir, remote):
    """Carica nel ramo "dati" i dati del Mac, sovrascrivendo quelli su GitHub (per partire o ripartire da capo)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "dati"
        dest.mkdir()
        for pattern in DATI:
            for src in sorted(data_dir.glob(pattern)):
                out = dest / src.relative_to(data_dir)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
        (dest / ".gitignore").write_text(DATI_IGNORA)
        git(dest, "init", "-q", "-b", "dati")
        git(dest, "config", "user.name", NOME_GIT)
        git(dest, "config", "user.email", EMAIL_GIT)
        git(dest, "remote", "add", "origin", remote)
        salva(dest)
        code, out = git(dest, "push", "-q", "-f", "origin", "HEAD:dati")
        return code == 0, "Dati caricati su GitHub" if code == 0 else out


if __name__ == "__main__":
    data_dir = Path(os.environ.get("F1LIVE_DATA_DIR") or Path.home() / "Library" / "Application Support" / "F1 Live")
    remote = json.loads((data_dir / "sito.json").read_text())["remote"]
    if sys.argv[1:] == ["codice"]:
        ok, msg = pubblica_codice(remote)
    elif sys.argv[1:] == ["dati"]:
        ok, msg = pubblica_dati(data_dir, remote)
    else:
        ok, msg = False, __doc__
    print(msg)
    sys.exit(0 if ok else 1)
