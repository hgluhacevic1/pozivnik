#!/usr/bin/env python3
"""
Pozivnik – istraživač izvora.

Za svaku općinu i svakog davaoca iz baze:
  1. nađe web stranicu (postojeća u bazi → Wikidata → uobičajene adrese općina u BiH),
  2. provjeri da stranica radi i da pripada toj općini (naziv u naslovu ili tekstu),
  3. pronađe rubriku s javnim pozivima / konkursima / natječajima.
Rezultat: izlaz/istrazivanje.json (+ .csv za pregled). Ništa ne upisuje u bazu.
"""
import concurrent.futures as cf, csv, json, os, re, sys, threading, time, unicodedata, urllib.parse
import requests
from bs4 import BeautifulSoup

def _konfig():
    """Javnu adresu i javni (anon) ključ baze čita iz index.html, isto što koristi i stranica."""
    t = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()
    u = re.search(r'supabaseUrl:\s*"([^"]+)"', t).group(1)
    k = re.search(r'supabaseAnonKey:\s*"([^"]+)"', t).group(1)
    return u, k


SUPABASE_URL, ANON = _konfig()
IZLAZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "izlaz")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "bs,hr;q=0.9,sr;q=0.8,en;q=0.6",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
_lok = threading.local()


def ses():
    if not hasattr(_lok, "s"):
        _lok.s = requests.Session(); _lok.s.headers.update(H)
    return _lok.s


def get(url, t=15):
    try:
        r = ses().get(url, timeout=t, allow_redirects=True)
        if r.status_code >= 400:
            return None
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        if "html" not in r.headers.get("content-type", "html"):
            return None
        return r
    except Exception:
        return None


def ascii_(s):
    s = s.replace("đ", "dj").replace("Đ", "Dj")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def kljuc_imena(n):
    n = ascii_(n)
    n = re.sub(r"\b(opcina|opstina|grad|city of|municipality of|municipality|city)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return " ".join(n.split())


def baza(tabela):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}?select=*&limit=5000",
                     headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"}, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------- Wikidata ----------
def wikidata():
    q = """SELECT ?item ?label ?web WHERE {
      ?item wdt:P17 wd:Q225; wdt:P856 ?web; wdt:P625 ?c.
      ?item rdfs:label ?label. FILTER(LANG(?label) IN ("bs","hr","sr","sh","en"))
    }"""
    try:
        r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                         headers={"User-Agent": "PozivnikBot/1.0 (pozivnik javni pozivi BiH)"}, timeout=90)
        r.raise_for_status()
        out = {}
        for b in r.json()["results"]["bindings"]:
            k = kljuc_imena(b["label"]["value"])
            out.setdefault(k, set()).add(b["web"]["value"])
        print("Wikidata:", len(out), "naziva s webom")
        return out
    except Exception as e:
        print("Wikidata greška:", e)
        return {}


def kandidati(naziv, rs):
    s = kljuc_imena(naziv)
    v = {s.replace(" ", ""), s.replace(" ", "-")}
    if " " in s:
        v.add(s.split()[0])
    out = []
    for x in v:
        if rs:
            out += [f"https://www.opstina{x}.com", f"https://opstina{x}.com", f"https://www.opstina-{x}.com",
                    f"https://www.opstina{x}.org", f"https://www.opstina-{x}.org", f"https://www.opstina{x}.net",
                    f"https://www.opstina{x}.rs.ba", f"https://www.{x}.rs.ba", f"https://www.grad{x}.org",
                    f"https://www.grad-{x}.org", f"https://{x}grad.org", f"https://www.{x}.ba",
                    f"https://www.opstina{x}.ba", f"https://www.{x}.org"]
        else:
            out += [f"https://www.{x}.gov.ba", f"https://{x}.gov.ba", f"https://www.opcina{x}.gov.ba",
                    f"https://opcina{x}.gov.ba", f"https://www.opcina-{x}.gov.ba", f"https://www.grad{x}.gov.ba",
                    f"https://www.{x}.ba", f"https://www.opcina{x}.ba", f"https://www.opcina-{x}.ba",
                    f"https://www.grad-{x}.ba", f"https://www.{x}.com.ba"]
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); res.append(u)
    return res


def pripada(r, naziv):
    """Stranica pripada općini ako se naziv pojavljuje u naslovu ili prvih 30k znakova."""
    k = kljuc_imena(naziv)
    if not k:
        return False
    tekst = ascii_(r.text[:30000])
    rijeci = [w for w in k.split() if len(w) > 2]
    return bool(rijeci) and all(w in tekst for w in rijeci)


RUB = re.compile(r"(javn\w*[\s\-_]*poziv|konkurs|natje[cč]aj|natjecaj|oglas|grant|poticaj|subvencij|"
                 r"call[s]?[\s\-_]*for|funding|tender|javne nabavke|pozivi)", re.I)
DOBRO = re.compile(r"(javn\w*[\s\-_]*poziv|konkurs|natje[cč]aj|natjecaj|call[s]?[\s\-_]*for|grant)", re.I)
LOSE = re.compile(r"(javne?[\s\-_]*nabavk|tender|zaposlen|posao|radna mjesta|prijem u radni|"
                  r"facebook|twitter|instagram|youtube|\.pdf$|\.docx?$|mailto:)", re.I)


def rubrike(r):
    """Kandidati za rubriku s pozivima na stranici, sortirani po pouzdanosti."""
    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        soup = BeautifulSoup(r.text, "html.parser")
    baza_host = urllib.parse.urlparse(r.url).netloc.replace("www.", "")
    nadj = {}
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text(" ").split())[:120]
        href = urllib.parse.urljoin(r.url, a["href"].strip())
        if not href.startswith("http"):
            continue
        host = urllib.parse.urlparse(href).netloc.replace("www.", "")
        if host != baza_host:
            continue
        hay = txt + " " + urllib.parse.unquote(href)
        if not RUB.search(hay) or LOSE.search(hay):
            continue
        s = 0
        if DOBRO.search(txt): s += 5
        if DOBRO.search(href): s += 3
        if re.search(r"(kategorij|category|/tag/|rubrik|section|lista|arhiv)", href, re.I): s += 2
        if len(txt) <= 40: s += 2          # meni stavka, ne naslov pojedinačnog poziva
        if re.search(r"\d{4}", txt): s -= 2  # vjerovatno pojedinačna objava
        if href.count("/") > 7: s -= 2
        if s <= 0:
            continue
        if href not in nadj or nadj[href][0] < s:
            nadj[href] = (s, txt)
    return sorted(([v[0], v[1], k] for k, v in nadj.items()), reverse=True)[:5]


def obradi_opcinu(o, wd):
    d = o.get("data") or {}
    naziv = d.get("naziv") or o.get("id")
    rs = (d.get("entitet") == "RS") or d.get("kanton") == "rs"
    res = {"tip": "opcina", "id": o["id"], "naziv": naziv, "kanton": d.get("kanton"), "web_staro": d.get("web", ""),
           "rubrika_staro": d.get("rubrika", ""), "web": "", "izvor_weba": "", "rubrike": [], "napomena": ""}
    probati = []
    if d.get("web"):
        probati.append((d["web"], "baza"))
    for u in sorted(wd.get(kljuc_imena(naziv), [])):
        probati.append((u, "wikidata"))
    probati += [(u, "pogodak") for u in kandidati(naziv, rs)]
    for u, izv in probati:
        r = get(u, 12)
        if r is None:
            continue
        if izv != "baza" and not pripada(r, naziv):
            continue
        res["web"], res["izvor_weba"] = r.url, izv
        res["rubrike"] = rubrike(r)
        break
    if not res["web"]:
        res["napomena"] = "web nije pronađen"
    return res


def obradi_davaoca(o):
    d = o.get("data") or {}
    res = {"tip": "davalac", "id": o["id"], "naziv": d.get("naziv", ""), "web_staro": d.get("web", ""),
           "web": "", "rubrike": [], "napomena": ""}
    if not d.get("web"):
        res["napomena"] = "nema weba u bazi"
        return res
    r = get(d["web"], 20)
    if r is None:
        res["napomena"] = "web ne radi ili blokira"
        return res
    res["web"] = r.url
    res["rubrike"] = rubrike(r)
    return res


def main():
    os.makedirs(IZLAZ, exist_ok=True)
    opc, dav = baza("opcine"), baza("donatori")
    print("Općina:", len(opc), "davalaca:", len(dav))
    wd = wikidata()
    rez = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        fut = [ex.submit(obradi_opcinu, o, wd) for o in opc] + [ex.submit(obradi_davaoca, o) for o in dav]
        for i, f in enumerate(cf.as_completed(fut), 1):
            try:
                rez.append(f.result())
            except Exception as e:
                print("greška:", e)
            if i % 25 == 0:
                print(i, "/", len(fut)); sys.stdout.flush()
    rez.sort(key=lambda x: (x["tip"], x["naziv"]))
    json.dump(rez, open(os.path.join(IZLAZ, "istrazivanje.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(IZLAZ, "istrazivanje.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tip", "naziv", "web", "izvor weba", "najbolja rubrika", "tekst linka", "napomena"])
        for x in rez:
            b = x["rubrike"][0] if x["rubrike"] else ["", "", ""]
            w.writerow([x["tip"], x["naziv"], x["web"], x.get("izvor_weba", ""), b[2], b[1], x["napomena"]])
    o = [x for x in rez if x["tip"] == "opcina"]
    print("Općine s webom:", sum(1 for x in o if x["web"]), "/", len(o),
          "| s rubrikom:", sum(1 for x in o if x["rubrike"]))


if __name__ == "__main__":
    main()
