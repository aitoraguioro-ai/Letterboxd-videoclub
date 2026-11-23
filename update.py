#!/usr/bin/env python3
"""Sincroniza movies.json con Letterboxd y TMDB.

Fuentes (de menor a mayor prioridad de estado):
  1. data/*.csv        Export de Letterboxd. watchlist*.csv = pendientes, el resto = vistas
                       (watched, ratings, diary, reviews...). Es la base histórica.
  2. RSS de Letterboxd Actividad reciente (~50 entradas). Siempre marca como vista.
  3. TMDB              Póster oficial, sinopsis y géneros (solo para lo que falte).

Reglas de fusión:
  * La clave de una película es título normalizado + año, así que nunca se duplica.
  * Vista gana a pendiente. Si una película de la watchlist aparece en el RSS o en los
    CSV de vistas, pasa a "watched" y no vuelve atrás.
  * Si hay watchlist*.csv, es la fuente de verdad de los pendientes: lo que ya no está
    en ese CSV (y no se ha visto) se elimina.
  * movies.json guarda tmdb_id, póster y géneros, así que TMDB solo se consulta
    para películas nuevas.

Solo usa la biblioteca estándar de Python 3.8+.
Variables de entorno: TMDB_API_KEY (clave v3 o token v4), LETTERBOXD_USER (por defecto aa33_uni).
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT = ROOT / "movies.json"
OVERRIDES = ROOT / "overrides.json"
USER = os.environ.get("LETTERBOXD_USER", "aa33_uni")
TMDB_BASE = os.environ.get("TMDB_BASE", "https://api.themoviedb.org/3")
UA = "Mozilla/5.0 (compatible; videoclub-sync/1.0)"
RECHECK_DAYS = 14

# Estanterías: com sta ani dra cri ter acc sci doc
GENRE_MAP = {99: "doc", 16: "ani", 27: "ter", 878: "sci", 28: "acc", 12: "acc",
             10752: "acc", 37: "acc", 80: "cri", 53: "cri", 9648: "cri", 35: "com"}
PRIORITY = (99, 16, 27, 878)
STANDUP_PREFIXES = [
    "dave chappelle", "neal brennan", "john mulaney", "chris rock", "george carlin",
    "ricky gervais", "louis c.k.", "jerry seinfeld", "norm macdonald", "joe rogan",
    "bill burr", "stewart lee", "tom segura", "jimmy carr", "pete davidson", "kevin hart",
    "andrew schulz", "mark normand", "shane gillis", "bo burnham", "hasan minhaj",
    "ali wong", "taylor tomlinson", "nate bargatze", "8:46",
]
FIELD_ORDER = ["id", "title", "year", "status", "genre", "rating", "review", "uri",
               "watched_date", "tmdb_id", "poster", "overview", "genres", "tmdb_checked", "src"]


def log(*a):
    print(*a, file=sys.stderr)


def slug(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s)


def key(title, year):
    return "%s|%s" % (slug(title), year or 0)


def parse_year(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else 0


def parse_rating(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def clean_text(t, cap=600):
    t = re.sub(r"https?://\S+", "", t or "").replace("\r", "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    if len(t) > cap:
        t = t[:cap].rsplit(" ", 1)[0] + "…"
    return t


# ---------------------------------------------------------------- CSV
def read_csvs():
    watched, pending, have_wl = [], [], False
    for path in sorted(DATA_DIR.glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "Name" not in rd.fieldnames:
                continue  # comments.csv, lists.csv...
            is_wl = path.stem.lower().startswith("watchlist")
            have_wl = have_wl or is_wl
            for row in rd:
                title = (row.get("Name") or "").strip()
                if not title:
                    continue
                rec = {"title": title, "year": parse_year(row.get("Year")),
                       "uri": (row.get("Letterboxd URI") or "").strip()}
                if is_wl:
                    pending.append(rec)
                    continue
                rec["rating"] = parse_rating(row.get("Rating"))
                rec["review"] = clean_text(row.get("Review"))
                rec["date"] = (row.get("Watched Date") or row.get("Date") or "").strip()
                watched.append(rec)
    return watched, pending, have_wl


# ---------------------------------------------------------------- RSS
def review_from_description(desc):
    out = []
    for p in re.findall(r"<p>(.*?)</p>", desc or "", re.S | re.I):
        if "<img" in p.lower():
            continue
        t = re.sub(r"<br\s*/?>", "\n", p, flags=re.I)
        t = html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        if not t or t.startswith("Watched on ") or t.startswith("This review may contain spoilers"):
            continue
        out.append(t)
    return clean_text("\n".join(out))


def fetch_rss(file=None):
    try:
        if file:
            raw = Path(file).read_bytes()
        else:
            req = urllib.request.Request("https://letterboxd.com/%s/rss/" % USER, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        root = ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        log("AVISO: no se pudo leer el RSS (%s). Se continúa sin él." % e)
        return []
    out = []
    for item in root.iter("item"):
        f = {}
        for ch in item:
            f[ch.tag.split("}")[-1]] = ch.text or ""
        title = f.get("filmTitle", "").strip()
        if not title:
            continue  # listas u otra actividad
        link = f.get("link", "")
        m = re.search(r"/film/([^/]+)/", link)
        tid = f.get("movieId", "").strip()
        out.append({
            "title": title,
            "year": parse_year(f.get("filmYear")),
            "uri": "https://letterboxd.com/film/%s/" % m.group(1) if m else "",
            "rating": parse_rating(f.get("memberRating")),
            "review": review_from_description(f.get("description")),
            "date": (f.get("watchedDate") or "").strip(),
            "tmdb_id": int(tid) if tid.isdigit() else None,
        })
    log("RSS: %d entradas con película." % len(out))
    return out


# ---------------------------------------------------------------- TMDB
class AuthError(Exception):
    pass


class Tmdb:
    def __init__(self, apikey):
        self.key = apikey
        self.bearer = apikey.startswith("eyJ")

    def get(self, path, **params):
        headers = {"Accept": "application/json", "User-Agent": UA}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.key
        else:
            params["api_key"] = self.key
        url = "%s%s?%s" % (TMDB_BASE, path, urllib.parse.urlencode(params))
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(int(e.headers.get("Retry-After", "2")) + 1)
                elif e.code in (401, 403):
                    raise AuthError("TMDB rechazó la clave (HTTP %d)" % e.code)
                elif e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                else:
                    return None
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 * (attempt + 1))
        return None


def pick(results, title, year):
    best, best_score = None, -99.0
    for r in results:
        names = (slug(r.get("title", "")), slug(r.get("original_title", "")))
        ry = parse_year((r.get("release_date") or "")[:4])
        s = 0.0
        if year and ry == year:
            s += 4
        elif year and ry and abs(ry - year) == 1:
            s += 2
        elif year:
            s -= 3
        if slug(title) in names:
            s += 3
        s += min(r.get("popularity", 0) or 0, 50) / 100.0
        if s > best_score:
            best, best_score = r, s
    return best if best_score >= 2 else None


def lookup(tm, m):
    found = None
    if m.get("tmdb_id"):
        d = tm.get("/movie/%d" % m["tmdb_id"], language="es-ES")
        if d:
            found = {"id": d["id"], "poster": d.get("poster_path"), "overview": d.get("overview"),
                     "genres": [g["id"] for g in d.get("genres", [])]}
    if not found:
        attempts = [m["year"], None] if m["year"] else [None]
        for yr in attempts:
            p = {"query": m["title"], "include_adult": "false", "language": "es-ES"}
            if yr:
                p["year"] = yr
            res = tm.get("/search/movie", **p)
            r = pick((res or {}).get("results", []), m["title"], m["year"])
            if r:
                found = {"id": r["id"], "poster": r.get("poster_path"), "overview": r.get("overview"),
                         "genres": r.get("genre_ids", [])}
                break
    if found and (not found["poster"] or not found["overview"]):
        d = tm.get("/movie/%d" % found["id"], language="en-US")
        if d:
            found["poster"] = found["poster"] or d.get("poster_path")
            found["overview"] = found["overview"] or d.get("overview")
            found["genres"] = found["genres"] or [g["id"] for g in d.get("genres", [])]
    return found


def needs_tmdb(m, today):
    if m.get("poster") and m.get("genres"):
        return False
    c = m.get("tmdb_checked")
    if c:
        try:
            if (today - dt.date.fromisoformat(c)).days < RECHECK_DAYS:
                return False
        except ValueError:
            pass
    return True


def enrich(movies, apikey):
    tm = Tmdb(apikey)
    today = dt.date.today()
    todo = [m for m in movies.values() if needs_tmdb(m, today)]
    log("TMDB: %d películas por consultar." % len(todo))
    done = 0
    for m in todo:
        try:
            f = lookup(tm, m)
        except AuthError as e:
            log("AVISO: %s. Se omite TMDB en esta ejecución." % e)
            return
        m["tmdb_checked"] = today.isoformat()
        if f:
            m["tmdb_id"] = f["id"]
            if f["poster"]:
                m["poster"] = f["poster"]
            if f["overview"]:
                m["overview"] = clean_text(f["overview"], 400)
            if f["genres"]:
                m["genres"] = f["genres"]
            done += 1
        else:
            log("  sin resultado: %s (%s)" % (m["title"], m["year"]))
        time.sleep(0.05)
    log("TMDB: %d encontradas." % done)


# ---------------------------------------------------------------- Fusión
def new_movie(title, year, status):
    return {"id": key(title, year), "title": title, "year": year, "status": status}


def shelf_for(m, overrides):
    if m["id"] in overrides:
        return overrides[m["id"]]
    low = m["title"].lower()
    if any(low.startswith(p) for p in STANDUP_PREFIXES):
        return "sta"
    ids = m.get("genres") or []
    for gid in PRIORITY:
        if gid in ids:
            return GENRE_MAP[gid]
    for gid in ids:
        if gid in GENRE_MAP:
            return GENRE_MAP[gid]
    return "dra"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rss", action="store_true", help="no leer el RSS")
    ap.add_argument("--no-tmdb", action="store_true", help="no consultar TMDB")
    ap.add_argument("--rss-file", help="leer el RSS desde un archivo (pruebas)")
    args = ap.parse_args()

    old = {}
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
    movies = {m["id"]: m for m in old.get("movies", [])}
    before = json.dumps(sorted(movies.values(), key=lambda m: m["id"]), ensure_ascii=False, sort_keys=True)
    overrides = {}
    if OVERRIDES.exists():
        overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")).get("genre", {})

    csv_watched, csv_pending, have_wl = read_csvs()
    rss = [] if args.no_rss else fetch_rss(args.rss_file)
    stats = {"nuevas": 0, "a_vista": 0, "eliminadas": 0}

    # 1) Vistas: CSV + RSS en orden de fecha (la última valoración gana)
    for rec in sorted(csv_watched + rss, key=lambda r: r.get("date", "")):
        k = key(rec["title"], rec["year"])
        m = movies.get(k)
        if not m:
            m = movies[k] = new_movie(rec["title"], rec["year"], "watched")
            stats["nuevas"] += 1
        elif m["status"] != "watched":
            m["status"] = "watched"
            m.pop("src", None)
            stats["a_vista"] += 1
        if rec.get("rating") is not None:
            m["rating"] = rec["rating"]
        if rec.get("review"):
            m["review"] = rec["review"]
        if rec.get("uri") and not m.get("uri"):
            m["uri"] = rec["uri"]
        if rec.get("tmdb_id"):
            m["tmdb_id"] = rec["tmdb_id"]
        if rec.get("date") and rec["date"] > m.get("watched_date", ""):
            m["watched_date"] = rec["date"]

    # 2) Pendientes: watchlist.csv es la fuente de verdad
    if have_wl:
        wl_keys = set()
        for rec in csv_pending:
            k = key(rec["title"], rec["year"])
            wl_keys.add(k)
            m = movies.get(k)
            if m and m["status"] == "watched":
                continue
            if not m:
                m = movies[k] = new_movie(rec["title"], rec["year"], "pending")
                m["src"] = "watchlist"
                stats["nuevas"] += 1
            if rec["uri"] and not m.get("uri"):
                m["uri"] = rec["uri"]
        for k in [k for k, m in movies.items()
                  if m["status"] == "pending" and m.get("src") == "watchlist" and k not in wl_keys]:
            del movies[k]
            stats["eliminadas"] += 1

    # 3) TMDB
    apikey = os.environ.get("TMDB_API_KEY", "").strip()
    if args.no_tmdb:
        pass
    elif not apikey:
        log("AVISO: falta TMDB_API_KEY. Se omiten pósters y géneros nuevos.")
    else:
        enrich(movies, apikey)

    # 4) Estantería de cada película
    for m in movies.values():
        m["genre"] = shelf_for(m, overrides)

    result = sorted(movies.values(), key=lambda m: (m["status"], m["title"].lower(), m["year"]))
    result = [{k: m[k] for k in FIELD_ORDER if m.get(k) not in (None, "", [])} for m in result]
    after = json.dumps(sorted(result, key=lambda m: m["id"]), ensure_ascii=False, sort_keys=True)
    log("Fusión: %d nuevas, %d pasan a vistas, %d eliminadas de la watchlist. Total: %d."
        % (stats["nuevas"], stats["a_vista"], stats["eliminadas"], len(result)))
    if before == after and OUT.exists():
        log("Sin cambios en movies.json.")
        return
    payload = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "user": USER, "count": len(result), "movies": result}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("movies.json actualizado.")


if __name__ == "__main__":
    main()
