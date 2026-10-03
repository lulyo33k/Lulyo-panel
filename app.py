#!/usr/bin/env python3
"""
Panneau maison pour gérer des apps Python / bots Discord.
Lancer :  pip install flask psutil  &&  python app.py   ->  http://localhost:8080
Premier lancement : crée ton compte administrateur sur la page de connexion.
"""
import hashlib, json, os, re, shutil, signal, subprocess, sys, threading, time
from pathlib import Path
import collections, platform, secrets
from flask import Flask, Response, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).parent.resolve()
APPS = ROOT / "apps"
APPS.mkdir(exist_ok=True)
IS_WIN = os.name == "nt"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ENTRY_GUESS = ["main.py", "bot.py", "app.py", "index.py", "run.py"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
procs = {}  # name -> {"state","proc","stop","thread"}


# ---------- sécurité / utilitaires ----------


def app_dir(name):
    if not NAME_RE.match(name or ""):
        raise ValueError("Nom invalide")
    d = APPS / name
    if not d.is_dir():
        raise FileNotFoundError("App introuvable")
    return d


def safe(name, rel=""):
    base = app_dir(name).resolve()
    p = (base / (rel or "").lstrip("/\\")).resolve()
    if p != base and base not in p.parents:
        raise ValueError("Chemin interdit")
    if p != base and p.relative_to(base).parts[0] == "venv":
        raise ValueError("Dossier venv protégé")
    return p


def venv_py(d):
    return d / "venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


def cfg(d):
    f = d / ".panel.json"
    return json.loads(f.read_text()) if f.exists() else {"entry": "main.py"}


def status(name):
    return procs.get(name, {}).get("state", "stopped")


@app.errorhandler(Exception)
def err(e):
    code = 404 if isinstance(e, FileNotFoundError) else 400
    return jsonify(error=str(e)), code


# ---------- process ----------
def kill_tree(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


def run_app(name, info):
    d = app_dir(name)
    log = open(d / "app.log", "wb", buffering=0)
    say = lambda t: log.write(f"[panel] {t}\n".encode())
    popen = dict(cwd=d, stdout=log, stderr=subprocess.STDOUT)
    if not IS_WIN:
        popen["start_new_session"] = True
    try:
        py = venv_py(d)
        if not py.exists():
            say("Création de l'environnement virtuel…")
            subprocess.run([sys.executable, "-m", "venv", str(d / "venv")], check=True, stdout=log, stderr=log)
        req = d / "requirements.txt"
        if req.exists():
            h = hashlib.md5(req.read_bytes()).hexdigest()
            marker = d / ".reqhash"
            if not marker.exists() or marker.read_text() != h:
                info["state"] = "installing"
                say("Installation des dépendances…")
                p = subprocess.Popen([str(py), "-m", "pip", "install", "-r", "requirements.txt"], **popen)
                info["proc"] = p
                if p.wait() != 0:
                    say("Échec de l'installation." if not info["stop"] else "Arrêté.")
                    return
                marker.write_text(h)
        if info["stop"]:
            return
        entry = cfg(d).get("entry", "main.py")
        if not (d / entry).exists():
            entry = next((e for e in ENTRY_GUESS if (d / e).exists()), None)
            if not entry:
                say("Aucun fichier de démarrage trouvé (main.py, bot.py…). Ajoute tes fichiers.")
                return
        say(f"Lancement de {entry}")
        p = subprocess.Popen([str(py), "-u", entry], stdin=subprocess.PIPE, **popen)
        info["proc"], info["state"] = p, "running"
        code = p.wait()
        say(f"Processus terminé (code {code})")
    except Exception as e:
        say(f"Erreur : {e}")
    finally:
        info["state"], info["proc"] = "stopped", None
        log.close()


def start(name):
    if status(name) != "stopped":
        return
    info = {"state": "starting", "proc": None, "stop": False}
    t = threading.Thread(target=run_app, args=(name, info), daemon=True)
    info["thread"] = t
    procs[name] = info
    t.start()


def stop(name):
    info = procs.get(name)
    if not info or info["state"] == "stopped":
        return
    info["stop"] = True
    kill_tree(info.get("proc"))
    info["thread"].join(15)


# ---------- API apps ----------
@app.get("/api/apps")
def list_apps():
    out = []
    u = cur_user()
    for d in sorted(p for p in APPS.iterdir() if p.is_dir() and (u["role"] == "admin" or p.name in u["apps"])):
        out.append({"name": d.name, "status": status(d.name), "entry": cfg(d).get("entry", "main.py")})
    return jsonify(out)


@app.post("/api/apps")
def create_app():
    name = (request.json or {}).get("name", "").strip()
    if not NAME_RE.match(name):
        raise ValueError("Nom invalide (lettres, chiffres, - et _ seulement)")
    d = APPS / name
    if d.exists():
        raise ValueError("Cette app existe déjà")
    d.mkdir()
    subprocess.run([sys.executable, "-m", "venv", str(d / "venv")], check=True)
    grant_owner(name)
    return jsonify(ok=True)


@app.delete("/api/apps/<name>")
def delete_app(name):
    d = app_dir(name)
    stop(name)
    procs.pop(name, None)
    purge_app(name)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify(ok=True)


@app.post("/api/apps/<name>/<action>")
def control(name, action):
    app_dir(name)
    if action == "start":
        start(name)
    elif action == "stop":
        stop(name)
    elif action == "restart":
        stop(name)
        start(name)
    else:
        raise ValueError("Action inconnue")
    return jsonify(ok=True, status=status(name))


@app.get("/api/apps/<name>/info")
def info(name):
    d = app_dir(name)
    return jsonify(name=name, status=status(name), entry=cfg(d).get("entry", "main.py"))


@app.put("/api/apps/<name>/config")
def set_cfg(name):
    d = app_dir(name)
    entry = (request.json or {}).get("entry", "main.py").strip() or "main.py"
    (d / ".panel.json").write_text(json.dumps({"entry": entry}))
    return jsonify(ok=True)


@app.get("/api/apps/<name>/logs")
def logs(name):
    f = app_dir(name) / "app.log"
    off = int(request.args.get("offset", 0))
    if not f.exists():
        return jsonify(text="", offset=0, reset=off > 0, status=status(name), stat=app_stats.get(name))
    size = f.stat().st_size
    reset = off > size
    if reset:
        off = 0
    with open(f, "rb") as fh:
        fh.seek(off)
        data = fh.read(200_000)
    return jsonify(text=data.decode("utf-8", "replace"), offset=off + len(data), reset=reset, status=status(name), stat=app_stats.get(name))


@app.post("/api/apps/<name>/stdin")
def stdin(name):
    p = procs.get(name, {}).get("proc")
    if not p or p.poll() is not None or not p.stdin or status(name) != "running":
        raise ValueError("L'app ne tourne pas")
    p.stdin.write(((request.json or {}).get("text", "") + "\n").encode())
    p.stdin.flush()
    return jsonify(ok=True)


# ---------- API fichiers ----------
@app.get("/api/apps/<name>/files")
def files(name):
    p = safe(name, request.args.get("path", ""))
    items = []
    for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if c.name in ("venv", "__pycache__", "app.log", ".panel.json", ".reqhash") and p == app_dir(name):
            continue
        if c.name == "__pycache__":
            continue
        items.append({"name": c.name, "dir": c.is_dir(), "size": c.stat().st_size if c.is_file() else 0})
    return jsonify(items)


@app.post("/api/apps/<name>/upload")
def upload(name):
    base = safe(name, request.form.get("path", ""))
    paths = request.form.getlist("paths")
    for f, rel in zip(request.files.getlist("files"), paths):
        dest = safe(name, str(base.relative_to(app_dir(name).resolve()) / rel))
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest)
    return jsonify(ok=True)


@app.get("/api/apps/<name>/file")
def read_file(name):
    p = safe(name, request.args.get("path", ""))
    if p.stat().st_size > 2_000_000:
        raise ValueError("Fichier trop gros pour l'éditeur")
    return jsonify(content=p.read_text("utf-8", "replace"))


@app.put("/api/apps/<name>/file")
def write_file(name):
    j = request.json
    safe(name, j["path"]).write_text(j["content"], "utf-8")
    return jsonify(ok=True)


@app.delete("/api/apps/<name>/file")
def del_file(name):
    p = safe(name, request.args.get("path", ""))
    if p == app_dir(name).resolve():
        raise ValueError("Interdit")
    shutil.rmtree(p) if p.is_dir() else p.unlink()
    return jsonify(ok=True)


@app.post("/api/apps/<name>/mkdir")
def mkdir(name):
    safe(name, request.json["path"]).mkdir(parents=True, exist_ok=True)
    return jsonify(ok=True)


# ---------- Comptes, sessions, monitoring ----------
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB, EV, KEY = DATA / "users.json", DATA / "events.jsonl", DATA / "secret.key"
lock = threading.RLock()
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
fails = {}
if not KEY.exists():
    KEY.write_text(secrets.token_hex(32))
app.secret_key = KEY.read_text().strip()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=7 * 86400)


def load():
    return json.loads(DB.read_text("utf-8")) if DB.exists() else {"open_signup": False, "users": {}}


def save(d):
    DB.write_text(json.dumps(d, indent=1), "utf-8")


def log(t, user="", detail=""):
    e = {"t": round(time.time(), 1), "type": t, "user": user, "ip": request.remote_addr or "",
         "ua": (request.headers.get("User-Agent") or "")[:200], "detail": detail}
    with lock, open(EV, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def cur_user():
    n = session.get("u")
    if not n:
        return None
    u = load()["users"].get(n)
    if not u:
        session.clear()
        return None
    return {"name": n, "role": u["role"], "apps": [a for a in u["apps"] if (APPS / a).is_dir()], "owned": u.get("owned", [])}


def purge_app(name):
    with lock:
        d = load()
        for u in d["users"].values():
            if name in u["apps"]:
                u["apps"].remove(name)
            if name in u.get("owned", []):
                u["owned"].remove(name)
        save(d)


def check_new(name, pw, d):
    if not USER_RE.match(name):
        raise ValueError("Nom invalide (3-32 caractères : lettres, chiffres, . _ -)")
    if name in d["users"]:
        raise ValueError("Ce nom est déjà pris")
    if len(pw) < 6:
        raise ValueError("Mot de passe trop court (6 caractères minimum)")


def grant_owner(name):
    """Un sous-utilisateur qui crée une app y a accès et en est propriétaire."""
    u = cur_user()
    if u and u["role"] != "admin":
        with lock:
            d = load()
            rec = d["users"][u["name"]]
            rec["apps"].append(name)
            rec.setdefault("owned", []).append(name)
            save(d)


PUBLIC = {"/api/me", "/api/login", "/api/register", "/api/logout"}


@app.before_request
def guard():
    p = request.path
    if not p.startswith("/api/") or p in PUBLIC:
        return
    u = cur_user()
    if not u:
        return jsonify(error="Non connecté"), 401
    if u["role"] == "admin":
        return
    m = re.match(r"^/api/apps/([^/]+)", p)
    if m:
        if request.method == "DELETE" and p == f"/api/apps/{m.group(1)}" and m.group(1) not in u["owned"]:
            return jsonify(error="Tu ne peux supprimer que tes propres apps"), 403
        if m.group(1) not in u["apps"]:
            return jsonify(error="Accès refusé à cette app"), 403
    elif p != "/api/apps":
        return jsonify(error="Réservé à l'administrateur"), 403


@app.get("/api/me")
def me():
    u, d = cur_user(), load()
    return jsonify(user=u and u["name"], role=u and u["role"], apps=u["apps"] if u else [], owned=u["owned"] if u else [],
                   setup=not d["users"], open_signup=d["open_signup"])


@app.post("/api/login")
def login():
    j = request.json or {}
    name, ip, now = str(j.get("username", "")).strip()[:32], request.remote_addr, time.time()
    fails[ip] = [t for t in fails.get(ip, []) if now - t < 600]
    if len(fails[ip]) >= 5:
        log("blocked", name, "Trop d'échecs, bloqué 10 min")
        return jsonify(error="Trop de tentatives, réessaie dans quelques minutes"), 429
    u = load()["users"].get(name)
    if not u or not check_password_hash(u["hash"], str(j.get("password", ""))):
        fails[ip].append(now)
        log("login_fail", name, "Identifiants incorrects")
        return jsonify(error="Identifiants incorrects"), 401
    fails.pop(ip, None)
    session.permanent = True
    session["u"] = name
    with lock:
        d = load()
        d["users"][name]["last_login"] = now
        save(d)
    log("login", name)
    return jsonify(ok=True)


@app.post("/api/register")
def register():
    j = request.json or {}
    name, pw = str(j.get("username", "")).strip(), str(j.get("password", ""))
    with lock:
        d = load()
        first = not d["users"]
        if not first and not d["open_signup"]:
            return jsonify(error="Les inscriptions sont fermées"), 403
        check_new(name, pw, d)
        d["users"][name] = {"hash": generate_password_hash(pw), "role": "admin" if first else "sub",
                            "apps": [], "created": time.time(), "last_login": time.time()}
        save(d)
    log("signup", name, "Premier compte (admin)" if first else "Inscription publique")
    session.permanent = True
    session["u"] = name
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    u = cur_user()
    if u:
        log("logout", u["name"])
    session.clear()
    return jsonify(ok=True)


def pub(n, u):
    return {"name": n, "role": u["role"], "apps": u["apps"], "last_login": u.get("last_login")}


@app.get("/api/users")
def users():
    d = load()
    return jsonify(users=[pub(n, u) for n, u in d["users"].items()], open_signup=d["open_signup"])


@app.post("/api/users")
def add_user():
    j = request.json or {}
    name = str(j.get("username", "")).strip()
    with lock:
        d = load()
        check_new(name, str(j.get("password", "")), d)
        apps = [a for a in j.get("apps", []) if NAME_RE.match(a) and (APPS / a).is_dir()]
        d["users"][name] = {"hash": generate_password_hash(j["password"]), "role": "sub", "apps": apps, "created": time.time()}
        save(d)
    log("user_create", cur_user()["name"], f"{name} → {', '.join(apps) or 'aucune app'}")
    return jsonify(ok=True)


@app.put("/api/users/<name>")
def edit_user(name):
    j = request.json or {}
    with lock:
        d = load()
        u = d["users"].get(name)
        if not u:
            raise FileNotFoundError("Utilisateur introuvable")
        if u["role"] == "admin":
            raise ValueError("Le compte admin ne se modifie pas ici")
        if "apps" in j:
            u["apps"] = [a for a in j["apps"] if NAME_RE.match(a) and (APPS / a).is_dir()]
        if j.get("password"):
            if len(j["password"]) < 6:
                raise ValueError("Mot de passe trop court (6 caractères minimum)")
            u["hash"] = generate_password_hash(j["password"])
        save(d)
    return jsonify(ok=True)


@app.delete("/api/users/<name>")
def del_user(name):
    with lock:
        d = load()
        if name not in d["users"] or d["users"][name]["role"] == "admin":
            raise ValueError("Suppression impossible")
        del d["users"][name]
        save(d)
    log("user_delete", cur_user()["name"], name)
    return jsonify(ok=True)


@app.put("/api/settings")
def settings():
    with lock:
        d = load()
        d["open_signup"] = bool((request.json or {}).get("open_signup"))
        save(d)
    return jsonify(ok=True)


@app.get("/api/events")
def events():
    ev = []
    if EV.exists():
        for line in EV.read_text("utf-8").splitlines():
            try:
                ev.append(json.loads(line))
            except ValueError:
                pass
    now = time.time()
    day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
    days = [day(now - i * 86400) for i in range(6, -1, -1)]
    agg = {d: {"d": d, "login": 0, "fail": 0, "signup": 0} for d in days}
    cnt = {"login": 0, "fail": 0, "signup": 0}
    for e in ev:
        k = {"login": "login", "login_fail": "fail", "signup": "signup"}.get(e["type"])
        if not k:
            continue
        if day(e["t"]) in agg:
            agg[day(e["t"])][k] += 1
        if now - e["t"] < 86400:
            cnt[k] += 1
    cnt["users"] = len(load()["users"])
    types = [t for t in request.args.get("type", "").split(",") if t]
    if types:
        ev = [e for e in ev if e["type"] in types]
    return jsonify(events=ev[::-1][:300], stats=cnt, days=[agg[d] for d in days])


@app.post("/api/apps/<name>/newfile")
def newfile(name):
    p = safe(name, (request.json or {}).get("path", ""))
    if p.exists():
        raise ValueError("Ce fichier existe déjà")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return jsonify(ok=True)


# ---------- Monitoring du serveur (psutil) ----------
try:
    import psutil
except ImportError:
    psutil = None
hist = collections.deque(maxlen=150)  # ~5 min, 1 point / 2 s
app_stats, _pc = {}, {}


def _proc(pid):
    if pid not in _pc:
        _pc[pid] = psutil.Process(pid)
    return _pc[pid]


def sampler():
    global app_stats
    ncpu = psutil.cpu_count() or 1
    psutil.cpu_percent(None)
    psutil.cpu_percent(None, percpu=True)
    last, lt = psutil.net_io_counters(), time.time()
    while True:
        time.sleep(2)
        try:
            now, n = time.time(), psutil.net_io_counters()
            dt = max(now - lt, 0.1)
            hist.append({"cpu": psutil.cpu_percent(None), "ram": psutil.virtual_memory().percent,
                         "rx": (n.bytes_recv - last.bytes_recv) / dt, "tx": (n.bytes_sent - last.bytes_sent) / dt})
            last, lt = n, now
            stats, seen = {}, set()
            for name, info in list(procs.items()):
                p = info.get("proc")
                if not p or p.poll() is not None or info["state"] != "running":
                    continue
                try:
                    root = _proc(p.pid)
                    cpu = rss = 0
                    for q in [root] + root.children(recursive=True):
                        q = _proc(q.pid)
                        seen.add(q.pid)
                        cpu += q.cpu_percent(None)
                        rss += q.memory_info().rss
                    stats[name] = {"pid": p.pid, "cpu": round(cpu / ncpu, 1), "rss": rss, "age": now - root.create_time()}
                except psutil.Error:
                    pass
            for pid in [k for k in _pc if k not in seen]:
                del _pc[pid]
            app_stats = stats
        except Exception:
            pass


def cpu_temp():
    try:
        for v in (psutil.sensors_temperatures() or {}).values():
            if v:
                return max(x.current for x in v)
    except Exception:
        pass


@app.get("/api/system")
def system():
    if not psutil:
        raise ValueError("Module psutil manquant : pip install psutil")
    vm, du, h = psutil.virtual_memory(), psutil.disk_usage(str(ROOT)), list(hist)
    return jsonify(
        cpu=h[-1]["cpu"] if h else 0, cores=psutil.cpu_count(), cores_pct=psutil.cpu_percent(None, percpu=True),
        ram={"percent": vm.percent, "used": vm.total - vm.available, "total": vm.total},
        disk={"percent": du.percent, "used": du.used, "total": du.total}, temp=cpu_temp(),
        load=list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        uptime=time.time() - psutil.boot_time(), node=platform.node(), os=f"{platform.system()} {platform.release()}",
        hist={k: [round(x[k], 1) for x in h] for k in ("cpu", "ram", "rx", "tx")},
        apps=[{"name": n, **s} for n, s in app_stats.items()])


if psutil:
    threading.Thread(target=sampler, daemon=True).start()


# ---------- Interface ----------
@app.get("/")
def index():
    return Response((ROOT / "index.html").read_text("utf-8"), mimetype="text/html")



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
