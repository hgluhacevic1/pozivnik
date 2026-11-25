#!/usr/bin/env python3
"""
Pozivnik – pratilac javnih poziva u BiH.

Svaki dan obiđe sve izvore iz izvori.json (stranice općina, ministarstava,
fondova, ambasada, portala i Google News pretrage), pronađe NOVE objave koje
liče na javni poziv, po želji pomoću AI izvuče podatke (rok, iznos, ko može
aplicirati) i pošalje izvještaj na Telegram i/ili email.

Pokretanje:
    python pratilac.py --baseline     # prvi put: zapamti sve postojeće, ništa ne javlja
    python pratilac.py                # svaki sljedeći put: javi samo novo
    python pratilac.py --samo "Tuzla" # testira samo izvore čiji naziv sadrži "Tuzla"

Rezultat se upisuje u folder izlaz/:
    novi_pozivi_YYYY-MM-DD.json   – spremno za uvoz u Pozivnik
    novi_pozivi_YYYY-MM-DD.csv    – za Excel
    izvjestaj_YYYY-MM-DD.html     – pregled za čitanje
    izvori_s_greskom.csv          – izvori koji ne rade (treba popraviti link)
"""
import argparse, csv, datetime as dt, hashlib, html, io, json, os, random, re, sqlite3, sys, threading, time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

OSNOVA = os.path.dirname(os.path.abspath(__file__))
IZLAZ = os.path.join(OSNOVA, "izlaz")
BAZA = os.path.join(OSNOVA, "stanje.sqlite")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
TIMEOUT = 30

# Riječi koje označavaju javni poziv (latinica i ćirilica, bs/hr/sr/en)
KLJUCNE = [
    r"javn\w* poziv", r"javn\w* konkurs", r"javn\w* natje[čc]aj", r"\bkonkurs", r"natje[čc]aj",
    r"\bgrant", r"sufinans", r"sufinanc", r"poticaj", r"podsticaj", r"dodjel\w* sredstava",
    r"raspodjel\w* sredstava", r"bespovratn", r"mali grant", r"call for (proposals|applications)",
    r"open call", r"funding opportunit", r"јавн\w* пози", r"јавн\w* конкурс", r"конкурс",
    r"суфинанс", r"подстицај", r"додјел\w* средстава", r"расподјел\w* средстава",
]
# Ono što NIJE poziv za sredstva (zapošljavanje, nabavke, imenovanja)
ISKLJUCI = [
    r"radn\w* mjest", r"prijem u radni", r"zapo[šs]ljavanj\w* (na|u) ", r"imenovanj", r"izbor i imenovanje",
    r"direktor", r"javn\w* nabavk", r"tender", r"prodaj\w* (stana|zemlji)", r"zakup", r"licitacij",
    r"volonter", r"pripravni", r"stipendij\w* za (rad|zaposl)", r"rezultat", r"lista (korisnika|odabran)",
    r"odluk\w* o (dodjeli|izboru)", r"запошљавањ", r"именовањ", r"набавк",
]
RE_KLJ = re.compile("|".join(KLJUCNE), re.I)
RE_ISK = re.compile("|".join(ISKLJUCI), re.I)


# ---------------------------------------------------------------- baza
def baza():
    c = sqlite3.connect(BAZA)
    c.execute("""create table if not exists vidjeno(
        kljuc text primary key, izvor text, naslov text, url text, prvi_put text)""")
    c.execute("""create table if not exists greske(
        izvor text primary key, broj integer, zadnja text, poruka text)""")
    # izvori koji su bar jednom uspješno pročitani; prvi uspjeh novog izvora = njegovo početno stanje
    nova = not c.execute("select 1 from sqlite_master where name='izvori_ok'").fetchone()
    c.execute("create table if not exists izvori_ok(izvor text primary key, prvi_put text)")
    if nova and c.execute("select count(*) from vidjeno").fetchone()[0]:
        # stara baza: izvori koji već imaju zapamćene objave imaju početno stanje
        izv = json.load(open(os.path.join(OSNOVA, "izvori.json"), encoding="utf-8"))
        imaju = {r[0] for r in c.execute("select distinct izvor from vidjeno")}
        c.executemany("insert or ignore into izvori_ok values(?, date('now'))",
                      [(i["url"],) for i in izv if i["naziv"] in imaju])
        c.commit()
    return c


def kljuc(url, naslov):
    u = urllib.parse.urldefrag(url)[0].rstrip("/").lower()
    return hashlib.sha1((u + "|" + (naslov or "").strip().lower()[:200]).encode()).hexdigest()


# ---------------------------------------------------------------- dohvat
ZAGLAVLJA = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bs,hr;q=0.9,sr;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
}
_lok = threading.local()


def sesija():
    # requests.Session nije siguran za više niti; svaka nit dobije svoju
    if not hasattr(_lok, "s"):
        _lok.s = requests.Session()
        _lok.s.headers.update(ZAGLAVLJA)
    return _lok.s


_vijesti_brava = threading.Lock()
_vijesti_zadnje = [0.0]


def dohvati(url, pokusaja=3):
    """GET s ponavljanjem: timeout/prekid veze/5xx/429 pokuša ponovo uz čekanje."""
    google = "news.google.com" in url
    zadnja = None
    for i in range(pokusaja):
        if google:  # Google News odbija previše upita odjednom (429) – jedan po jedan, s razmakom
            with _vijesti_brava:
                cekaj = 1.5 - (time.time() - _vijesti_zadnje[0])
                if cekaj > 0:
                    time.sleep(cekaj)
                _vijesti_zadnje[0] = time.time()
        try:
            r = sesija().get(url, timeout=TIMEOUT + 15 * i, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504) and i < pokusaja - 1:
                time.sleep((10 if r.status_code == 429 else 4) * (i + 1) + random.random() * 3)
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            zadnja = e
            if i < pokusaja - 1:
                time.sleep(3 * (i + 1))
    raise zadnja


def je_poziv(tekst):
    return bool(RE_KLJ.search(tekst)) and not RE_ISK.search(tekst)


def linkovi_sa_stranice(izvor):
    """Vrati listu (naslov, url) objava koje liče na javni poziv."""
    r = dohvati(izvor["url"])
    tip = r.headers.get("content-type", "")
    if "xml" in tip or izvor["url"].endswith("/feed/"):
        return iz_rss(r.content)
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup(["script", "style", "nav", "footer", "header"]):
        s.decompose()
    nadjeno = []
    for a in soup.find_all("a", href=True):
        naslov = " ".join(a.get_text(" ", strip=True).split())
        url = urllib.parse.urljoin(r.url, a["href"])
        if not url.startswith("http"):
            continue
        # gledamo tekst linka, njegov naziv fajla i okolni tekst (npr. naslov iznad PDF-a)
        okolina = naslov
        if len(naslov) < 15 and a.parent is not None:
            okolina = " ".join(a.parent.get_text(" ", strip=True).split())[:300]
        ime_fajla = urllib.parse.unquote(url.rsplit("/", 1)[-1]).replace("-", " ").replace("_", " ")
        if je_poziv(okolina + " " + ime_fajla):
            nadjeno.append((okolina[:300] or ime_fajla, url))
    return nadjeno


def iz_rss(sadrzaj):
    soup = BeautifulSoup(sadrzaj, "xml")
    out = []
    for it in soup.find_all("item"):
        t = it.title.get_text(strip=True) if it.title else ""
        l = it.link.get_text(strip=True) if it.link else ""
        if t and l and je_poziv(t):
            out.append((t, l))
    return out


def iz_vijesti(upit):
    """Google News RSS – hvata pozive koje mediji prenesu, i za općine bez poznate stranice."""
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": upit + " when:14d", "hl": "bs", "gl": "BA", "ceid": "BA:bs"})
    return iz_rss(dohvati(url).content)


def rezervni_upit(izvor):
    """Ako stranica blokira automatske posjete, traži njene objave preko Google News."""
    if izvor.get("rezerva"):
        return izvor["rezerva"]
    domena = urllib.parse.urlparse(izvor["url"]).netloc.removeprefix("www.").removeprefix("www2.")
    return f'site:{domena} (poziv OR konkurs OR natječaj OR grant OR "call for")'


def obradi_izvor(izvor):
    if izvor["tip"] == "vijesti":
        return izvor, iz_vijesti(izvor["url"][len("news:"):]), None
    try:
        return izvor, linkovi_sa_stranice(izvor), None
    except Exception as e:
        # stranica ne radi ili nas blokira: probaj rezervu; ako i ona ne uspije, prijavi izvornu grešku
        try:
            return izvor, iz_vijesti(rezervni_upit(izvor)), f"rezerva ({str(e)[:80]})"
        except Exception:
            raise e


# ---------------------------------------------------------------- AI izvlačenje
def tekst_objave(url, max_znakova=12000):
    try:
        r = dohvati(url)
    except Exception:
        return ""
    tip = r.headers.get("content-type", "")
    if "pdf" in tip or url.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(r.content))
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:6])[:max_znakova]
        except Exception:
            return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup(["script", "style", "nav", "footer", "header"]):
        s.decompose()
    return " ".join(soup.get_text(" ").split())[:max_znakova]


AI_UPUTA = """Ti si asistent koji čita objave institucija u Bosni i Hercegovini.
Iz teksta ispod izvuci podatke o javnom pozivu za dodjelu sredstava i vrati SAMO JSON:
{"je_poziv_za_sredstva": true/false,
 "naslov": "", "davalac": "", "iznos": "", "rok": "YYYY-MM-DD ili prazno",
 "ko": "ko može aplicirati", "podrucje": "jedno od: bih, fbih, rs, brcko, usk, pk, tk, zdk, bpk, sbk, hnk, zhk, ks, k10, vise",
 "teme": ["udruzenja","sport","mladi","zene","kultura","socijala","mediji","turizam","ustanove","firme"],
 "napomena": "1-2 rečenice: šta se finansira i kako se aplicira"}
Pravila: ne izmišljaj. Ako rok ili iznos nisu navedeni, ostavi prazno.
je_poziv_za_sredstva je false za zapošljavanje, javne nabavke, imenovanja, rezultate i liste korisnika.
"""


def ai_izvuci(naslov, url, tekst):
    kljuc_api = os.environ.get("ANTHROPIC_API_KEY")
    if not kljuc_api or not tekst:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": kljuc_api, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("POZIVNIK_MODEL", "claude-haiku-5-5"), "max_tokens": 800,
                  "messages": [{"role": "user", "content": f"{AI_UPUTA}\nNaslov: {naslov}\nURL: {url}\n\nTekst:\n{tekst}"}]},
            timeout=90)
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"  AI greška za {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- obavijesti
def posalji_telegram(poruka):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    for dio in [poruka[i:i + 3900] for i in range(0, len(poruka), 3900)]:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": dio, "disable_web_page_preview": True}, timeout=30)
        except Exception as e:
            print("Telegram greška:", e, file=sys.stderr)


def posalji_email(naslov, html_tijelo):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(html_tijelo, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = naslov, os.environ["SMTP_FROM"], os.environ["EMAIL_TO"]
    try:
        with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), timeout=60) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    except Exception as e:
        print("Email greška:", e, file=sys.stderr)


# ---------------------------------------------------------------- Supabase
def upisi_u_supabase(pozivi):
    """Upiše nove pozive u Supabase kao kandidate (odobren=false). Urednik ih pregleda u 'Uredi'."""
    url, kljuc_s = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not kljuc_s or not pozivi:
        return 0
    redovi = [{"id": "auto-" + hashlib.sha1(p["izvor"].encode()).hexdigest()[:16],
               "data": {k: v for k, v in p.items()}, "odobren": False} for p in pozivi]
    try:
        r = requests.post(url.rstrip("/") + "/rest/v1/pozivi", json=redovi, timeout=60, headers={
            "apikey": kljuc_s, "Authorization": "Bearer " + kljuc_s, "Content-Type": "application/json",
            # postojeće (već pregledane) pozive ne diramo
            "Prefer": "resolution=ignore-duplicates,return=minimal"})
        r.raise_for_status()
        return len(redovi)
    except Exception as e:
        print("Supabase greška:", e, getattr(e, "response", None) and e.response.text, file=sys.stderr)
        return 0


# ---------------------------------------------------------------- glavno
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="zapamti sve postojeće objave bez javljanja")
    ap.add_argument("--samo", default="", help="obradi samo izvore čiji naziv sadrži ovaj tekst")
    ap.add_argument("--niti", type=int, default=8)
    a = ap.parse_args()

    izvori = json.load(open(os.path.join(OSNOVA, "izvori.json"), encoding="utf-8"))
    if a.samo:
        izvori = [i for i in izvori if a.samo.lower() in i["naziv"].lower()]
    os.makedirs(IZLAZ, exist_ok=True)
    db = baza()
    if not a.samo:  # izvori koji su izbačeni ili im je promijenjen link više se ne vode kao greška
        sve = {i["url"] for i in json.load(open(os.path.join(OSNOVA, "izvori.json"), encoding="utf-8"))}
        for (u,) in db.execute("select izvor from greske").fetchall():
            if u not in sve:
                db.execute("delete from greske where izvor=?", (u,))
    danas = dt.date.today().isoformat()
    novi, greske, rezerve = [], [], []

    print(f"Obilazim {len(izvori)} izvora…")
    with ThreadPoolExecutor(max_workers=a.niti) as ex:
        futs = {ex.submit(obradi_izvor, i): i for i in izvori}
        for f in as_completed(futs):
            izv = futs[f]
            try:
                _, nadjeno, napomena = f.result()
                db.execute("delete from greske where izvor=?", (izv["url"],))
                if napomena:
                    rezerve.append((izv["naziv"], izv["url"], napomena))
            except Exception as e:
                greske.append((izv["naziv"], izv["url"], str(e)[:200]))
                db.execute("""insert into greske values(?,1,?,?) on conflict(izvor) do update
                              set broj=broj+1, zadnja=excluded.zadnja, poruka=excluded.poruka""",
                           (izv["url"], danas, str(e)[:200]))
                continue
            prvi = not db.execute("select 1 from izvori_ok where izvor=?", (izv["url"],)).fetchone()
            if prvi:
                db.execute("insert into izvori_ok values(?,?)", (izv["url"], danas))
            for naslov, url in nadjeno:
                k = kljuc(url, naslov)
                if db.execute("select 1 from vidjeno where kljuc=?", (k,)).fetchone():
                    continue
                db.execute("insert into vidjeno values(?,?,?,?,?)", (k, izv["naziv"], naslov, url, danas))
                if not a.baseline and not prvi:  # prvi uspjeh izvora samo pamti, ne javlja
                    novi.append({"izvor": izv["naziv"], "grupa": izv["grupa"], "podrucje_izvora": izv.get("podrucje", ""),
                                 "naslov": naslov, "url": url})
    db.commit()

    if a.baseline:
        n = db.execute("select count(*) from vidjeno").fetchone()[0]
        print(f"Početno stanje zapamćeno: {n} objava. Od sljedećeg pokretanja javljam samo novo.")
        return

    # isti poziv često objave i općina i mediji – spoji po URL-u
    jedinstveni = {}
    for n in novi:
        jedinstveni.setdefault(urllib.parse.urldefrag(n["url"])[0], n)
    novi = list(jedinstveni.values())

    # AI obrada (ako postoji ključ)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(f"AI čita {len(novi)} novih objava…")
        for n in novi:
            podaci = ai_izvuci(n["naslov"], n["url"], tekst_objave(n["url"]))
            if podaci:
                n["ai"] = podaci
            time.sleep(0.5)
        novi = [n for n in novi if n.get("ai", {}).get("je_poziv_za_sredstva", True)]

    # zapis rezultata
    pozivi = []
    for n in novi:
        ai = n.get("ai", {})
        pozivi.append({
            "naslov": ai.get("naslov") or n["naslov"], "davalac": ai.get("davalac") or n["izvor"],
            "iznos": ai.get("iznos", ""), "status": "provjeriti", "rok": ai.get("rok", ""),
            "rokProcjena": "", "rokNapomena": "" if ai.get("rok") else "Rok provjeriti u pozivu",
            "podrucje": ai.get("podrucje") or n["podrucje_izvora"] or "bih", "objavljeno": danas,
            "ko": ai.get("ko", ""), "napomena": ai.get("napomena", ""), "osnova": "",
            "izvor": n["url"], "provjereno": "", "teme": ai.get("teme", []),
            "_pronadjeno_na": n["izvor"],
        })
    json.dump(pozivi, open(os.path.join(IZLAZ, f"novi_pozivi_{danas}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(IZLAZ, f"novi_pozivi_{danas}.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Naslov", "Davalac", "Rok", "Iznos", "Ko može", "Link", "Pronađeno na"])
        for p in pozivi:
            w.writerow([p["naslov"], p["davalac"], p["rok"], p["iznos"], p["ko"], p["izvor"], p["_pronadjeno_na"]])
    with open(os.path.join(IZLAZ, "izvori_s_greskom.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Izvor", "URL", "Uzastopnih grešaka", "Poruka"])
        for izvor, broj, _, poruka in db.execute("select izvor,broj,zadnja,poruka from greske order by broj desc"):
            w.writerow([next((i["naziv"] for i in izvori if i["url"] == izvor), ""), izvor, broj, poruka])

    redovi = "".join(
        f"<li><a href='{html.escape(p['izvor'])}'>{html.escape(p['naslov'])}</a><br>"
        f"<small>{html.escape(p['davalac'])}{' · rok ' + html.escape(p['rok']) if p['rok'] else ''}"
        f"{' · ' + html.escape(p['iznos']) if p['iznos'] else ''}</small></li>" for p in pozivi)
    izvj = (f"<h2>Pozivnik – novi pozivi {danas}</h2><p>Obiđeno izvora: {len(izvori)}. "
            f"Novih objava: {len(pozivi)}. Izvora s greškom: {len(greske)}. Čitano preko rezerve: {len(rezerve)}.</p><ol>{redovi or '<li>Nema novih objava.</li>'}</ol>"
            "<p><small>Sve objave su kandidati: u Pozivniku su u tabu Uredi, označene „Novo iz pratioca”. "
            "Prije odobravanja provjeri rok i uslove u originalu.</small></p>")
    open(os.path.join(IZLAZ, f"izvjestaj_{danas}.html"), "w", encoding="utf-8").write(izvj)

    upisano = upisi_u_supabase(pozivi)
    if upisano:
        print(f"Upisano u Supabase kao kandidati za pregled: {upisano}")
    print(f"Gotovo: {len(pozivi)} novih objava, {len(greske)} izvora s greškom, {len(rezerve)} preko rezerve (Google News). Rezultati u {IZLAZ}")
    if pozivi:
        txt = f"Pozivnik – {len(pozivi)} novih objava ({danas})\n\n" + "\n\n".join(
            f"• {p['naslov'][:180]}\n  {p['davalac']}{' · rok ' + p['rok'] if p['rok'] else ''}\n  {p['izvor']}"
            for p in pozivi[:40])
        posalji_telegram(txt)
        posalji_email(f"Pozivnik: {len(pozivi)} novih objava ({danas})", izvj)


def odrzi_supabase():
    """Besplatni Supabase pauzira projekat nakon 7 dana bez aktivnosti; jedan mali upit dnevno to sprječava."""
    try:
        t = open(os.path.join(OSNOVA, "index.html"), encoding="utf-8").read()
        u = re.search(r'supabaseUrl:\s*"([^"]+)"', t).group(1)
        k = re.search(r'supabaseAnonKey:\s*"([^"]+)"', t).group(1)
        r = requests.get(u + "/rest/v1/pozivi?select=id&limit=1", headers={"apikey": k, "Authorization": "Bearer " + k}, timeout=20)
        print("Supabase:", r.status_code)
    except Exception as e:
        print("Supabase nije dostupan:", e)


if __name__ == "__main__":
    main()
    odrzi_supabase()
