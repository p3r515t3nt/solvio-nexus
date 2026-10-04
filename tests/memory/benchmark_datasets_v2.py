"""STEP 21.1 — zwei DISJUNKTE synthetische Datensaetze (PHASE 2/3).

DEV (Tuning) und HOLDOUT (final) nutzen NICHT-ueberlappende Entitaeten, Fakten,
Queries und Templates. Der HOLDOUT wird waehrend des Tunings nicht betrachtet.
Nur erfundene Daten. Vier Query-Klassen je Datensatz:

  SEMANTIC  Paraphrase/Synonym/indirekt/cross-language (FTS-schwach)
  EXACT     Namen/IDs/Rechnungsnummern/Zahlen (FTS-stark)
  NEGATIVE  Fakt existiert NICHT (gold=None), aehnlicher Distraktor vorhanden
  TEMPORAL  alte vs. aktuelle Wahrheit (Supersession); gold = aktueller Fakt

seed + version + SHA-256 werden mitgeliefert.
"""
from __future__ import annotations

import hashlib
import json
import random

VERSION = "v2.1"

# ---- DEV-Pool (Entitaeten NUR hier) --------------------------------------
DEV = {
    "seed": 21010001,
    "semantic": [  # (subject, fact, paraphrase-query)
        ("u:beverage", "Der Nutzer bevorzugt morgens einen doppelten Espresso.",
         "Was für ein Heißgetränk mag er früh am Tag?"),
        ("u:transport", "Der Nutzer pendelt täglich mit der Straßenbahn ins Büro.",
         "Wie gelangt er zur Arbeit?"),
        ("u:hobby", "Der Nutzer verbringt Wochenenden am liebsten mit Bergsteigen.",
         "Welche Freizeitbeschäftigung zieht er am Wochenende vor?"),
        ("u:food", "Der Nutzer meidet scharfe Gerichte konsequent.",
         "Verträgt er würziges Essen?"),
        ("u:music", "Der Nutzer schwärmt für barocke Orchestermusik.",
         "Welche Art von Klängen begeistert ihn?"),
        ("u:reading", "Der Nutzer verschlingt am liebsten historische Romane.",
         "Welche Literatur liest er bevorzugt?"),
        ("u:health", "Der Nutzer treibt aus Rückengründen regelmäßig Schwimmsport.",
         "Warum geht er häufig ins Becken?"),
        ("u:lang", "Der Nutzer beherrscht die italienische Sprache verhandlungssicher.",
         "Which language besides German is he very good at?"),
        ("u:weather", "Der Nutzer fühlt sich bei kühlem Nebelwetter am wohlsten.",
         "Welches Klima ist ihm am angenehmsten?"),
        ("u:work", "Der Nutzer entwirft beruflich Brückenkonstruktionen.",
         "Was ist sein Fachgebiet im Job?"),
        ("u:pet", "Der Nutzer kümmert sich liebevoll um zwei Wellensittiche.",
         "Welche Tiere hält er zuhause?"),
        ("u:sleep", "Der Nutzer legt sich gewöhnlich erst nach Mitternacht schlafen.",
         "Ist er ein Frühschläfer?"),
    ],
    "exact": [  # (subject, fact-mit-code, exakte-query)
        ("proj:DEVX", "Projekt DEVX-Falkenstein hat das Budget-Kürzel BK-77device.",
         "Wie lautet das Budget-Kürzel von Projekt DEVX-Falkenstein?"),
        ("inv:9931", "Die Rechnung INV-DEV-9931 über 2480 Euro ist noch offen.",
         "Welcher Betrag steht auf Rechnung INV-DEV-9931?"),
        ("person:Quenzer", "Frau Quenzer ist die Ansprechpartnerin für Vertrag V-4471.",
         "Wer ist zuständig für Vertrag V-4471?"),
        ("dev:serial", "Der Server Nordpol-7 trägt die Seriennummer SNX-DEV-30291.",
         "Wie lautet die Seriennummer von Server Nordpol-7?"),
        ("room:B214", "Das Meeting findet in Raum B214 im Gebäude Habichtsweg statt.",
         "In welchem Raum ist das Meeting im Gebäude Habichtsweg?"),
        ("code:AX88", "Der Zugangscode für das Labor lautet AX88-Kestrel-2207.",
         "Wie lautet der Zugangscode AX88 für das Labor?"),
        ("flight:LH417", "Der Rückflug ist mit LH417 am 12. November gebucht.",
         "Mit welchem Flug LH417 ist der Rückflug?"),
        ("part:GR55", "Das Ersatzteil GR55-Drallberg passt nur in Modell Zephyr-3.",
         "Wofür passt das Ersatzteil GR55-Drallberg?"),
        ("iban:tail", "Die Erstattung geht auf das Konto mit Endziffern 6642119.",
         "Auf welche Kontoendziffern 6642119 geht die Erstattung?"),
        ("ticket:T8890", "Support-Ticket T8890 betrifft den Drucker im dritten Stock.",
         "Was betrifft Support-Ticket T8890?"),
        ("lot:LOT4501", "Charge LOT4501 wurde am 3. Mai freigegeben.",
         "Wann wurde Charge LOT4501 freigegeben?"),
        ("meet:M-33", "Das Kick-off M-33 startet pünktlich um 08:45 Uhr.",
         "Um wie viel Uhr startet Kick-off M-33?"),
    ],
    "temporal": [  # (subject, old_fact, new_fact, current-query)
        ("u:city", "Der Nutzer wohnte früher in Regensburg.",
         "Der Nutzer wohnt inzwischen in Freiburg.",
         "Wo lebt der Nutzer aktuell?"),
        ("u:role", "Der Nutzer war zunächst als Tester angestellt.",
         "Der Nutzer arbeitet mittlerweile als Teamleiter.",
         "Welche Position hat er derzeit?"),
        ("u:carbrand", "Der Nutzer fuhr lange einen Kombi der Marke Falkon.",
         "Der Nutzer fährt jetzt ein Lastenrad statt Auto.",
         "Womit ist der Nutzer heute unterwegs?"),
        ("u:phoneplan", "Der Nutzer hatte früher einen Prepaid-Tarif.",
         "Der Nutzer nutzt inzwischen einen Flatrate-Vertrag.",
         "Welchen Mobilfunktarif hat er momentan?"),
        ("u:diet2", "Der Nutzer aß früher viel Rindfleisch.",
         "Der Nutzer lebt inzwischen pescetarisch.",
         "Wie ernährt sich der Nutzer heute?"),
        ("u:gym", "Der Nutzer trainierte anfangs im Fitnessstudio Eisberg.",
         "Der Nutzer trainiert jetzt ausschließlich zuhause.",
         "Wo trainiert der Nutzer aktuell?"),
        ("u:tooltime", "Der Nutzer setzte früher auf das Tool Kranich.",
         "Der Nutzer nutzt mittlerweile das Tool Distelfink.",
         "Welches Tool verwendet er derzeit?"),
        ("u:coffee2", "Der Nutzer trank früher literweise Filterkaffee.",
         "Der Nutzer trinkt inzwischen höchstens eine Tasse Espresso.",
         "Wie viel Kaffee trinkt er heute?"),
    ],
    "negative": [  # (query-ohne-passenden-Fakt, aehnlicher-Distraktor-Fakt)
        ("Welche Katzenrasse besitzt der Nutzer?",
         "Der Nutzer hat allgemein eine Vorliebe für ruhige Haustiere."),
        ("Wie heißt der Bruder des Nutzers?",
         "Der Nutzer erwähnt gelegentlich Familienfeiern im Sommer."),
        ("Welchen Motorradführerschein hat der Nutzer?",
         "Der Nutzer interessiert sich vage für Fahrzeugtechnik."),
        ("In welchem Chor singt der Nutzer?",
         "Der Nutzer hört zwar gerne Musik, tritt aber nie selbst auf."),
        ("Welche Kreditkartennummer nutzt der Nutzer?",
         "Der Nutzer spricht ungern über finanzielle Details."),
        ("Wann hat der Nutzer in Tokio gelebt?",
         "Der Nutzer war einmal kurz beruflich in Asien unterwegs."),
        ("Welches Segelboot besitzt der Nutzer?",
         "Der Nutzer findet Wassersport im Urlaub interessant."),
        ("Wie lautet das Passwort des Nutzers?",
         "Der Nutzer legt großen Wert auf digitale Sicherheit."),
        ("Welche Programmiersprache hat der Nutzer erfunden?",
         "Der Nutzer nutzt beruflich diverse gängige Werkzeuge."),
        ("Wann heiratet der Nutzer?",
         "Der Nutzer besucht hin und wieder Hochzeiten von Freunden."),
    ],
    "distractor_cities": ["Regenwalde", "Nordkirchen", "Talberg", "Seewinkel", "Moorbach"],
}

# ---- HOLDOUT-Pool (voellig andere Entitaeten) ----------------------------
HOLDOUT = {
    "seed": 21029999,
    "semantic": [
        ("u:beverage", "Der Nutzer greift nachmittags gern zu grünem Tee.",
         "Welches Getränk wählt er am Nachmittag?"),
        ("u:transport", "Der Nutzer erreicht die Firma meist per Tretroller.",
         "Womit legt er den Arbeitsweg zurück?"),
        ("u:hobby", "Der Nutzer widmet freie Abende der Aquarellmalerei.",
         "Welchem kreativen Zeitvertreib geht er abends nach?"),
        ("u:food", "Der Nutzer verzichtet strikt auf Milchprodukte.",
         "Isst er Käse und Joghurt?"),
        ("u:music", "Der Nutzer begeistert sich für karibische Trommelrhythmen.",
         "Welche Klänge reißen ihn mit?"),
        ("u:reading", "Der Nutzer bevorzugt Kriminalliteratur aus Skandinavien.",
         "Welche Bücher liest er am liebsten?"),
        ("u:health", "Der Nutzer macht wegen der Gelenke lieber Radfahren.",
         "Warum sitzt er oft im Sattel?"),
        ("u:lang", "Der Nutzer spricht fließend Portugiesisch.",
         "Which foreign language is he fluent in?"),
        ("u:weather", "Der Nutzer blüht bei heißem Wüstenklima richtig auf.",
         "Welches Wetter tut ihm besonders gut?"),
        ("u:work", "Der Nutzer plant beruflich Bewässerungsanlagen.",
         "In welchem Bereich arbeitet er fachlich?"),
        ("u:pet", "Der Nutzer pflegt zuhause ein kleines Meerschweinchen-Rudel.",
         "Welche Tiere leben bei ihm?"),
        ("u:sleep", "Der Nutzer steht üblicherweise vor Sonnenaufgang auf.",
         "Ist er eher ein Langschläfer?"),
    ],
    "exact": [
        ("proj:HOLT", "Projekt HOLT-Silbermond nutzt das Kostenkürzel KK-51harbor.",
         "Wie lautet das Kostenkürzel von Projekt HOLT-Silbermond?"),
        ("inv:2207", "Die Rechnung INV-HLD-2207 über 5190 Euro wurde bezahlt.",
         "Welcher Betrag steht auf Rechnung INV-HLD-2207?"),
        ("person:Radke", "Herr Radke verantwortet den Wartungsplan W-8820.",
         "Wer verantwortet den Wartungsplan W-8820?"),
        ("dev:serial", "Das Gerät Südpol-2 hat die Seriennummer SNY-HLD-77450.",
         "Wie lautet die Seriennummer von Gerät Südpol-2?"),
        ("room:C309", "Der Workshop läuft in Raum C309 im Gebäude Lerchenfeld.",
         "In welchem Raum ist der Workshop im Gebäude Lerchenfeld?"),
        ("code:QT12", "Der Tresorcode im Archiv lautet QT12-Marlow-6608.",
         "Wie lautet der Tresorcode QT12 im Archiv?"),
        ("flight:BA905", "Der Hinflug erfolgt mit BA905 am 4. Februar.",
         "Mit welchem Flug BA905 ist der Hinflug?"),
        ("part:HL92", "Das Bauteil HL92-Rabenhorst passt in Modell Auriga-9.",
         "Wofür passt das Bauteil HL92-Rabenhorst?"),
        ("iban:tail", "Die Auszahlung geht auf das Konto mit Endziffern 5530884.",
         "Auf welche Kontoendziffern 5530884 geht die Auszahlung?"),
        ("ticket:T3345", "Support-Ticket T3345 betrifft den Scanner im Erdgeschoss.",
         "Was betrifft Support-Ticket T3345?"),
        ("lot:LOT7788", "Charge LOT7788 wurde am 19. August gesperrt.",
         "Wann wurde Charge LOT7788 gesperrt?"),
        ("meet:M-71", "Das Review M-71 beginnt exakt um 14:15 Uhr.",
         "Um wie viel Uhr beginnt Review M-71?"),
    ],
    "temporal": [
        ("u:city", "Der Nutzer wohnte früher in Kassel.",
         "Der Nutzer wohnt inzwischen in Rostock.",
         "In welcher Stadt wohnt er heutzutage?"),
        ("u:role", "Der Nutzer war anfangs Praktikant.",
         "Der Nutzer ist mittlerweile Abteilungsleiter.",
         "Was ist seine jetzige berufliche Rolle?"),
        ("u:carbrand", "Der Nutzer besaß lange ein Coupé der Marke Wisent.",
         "Der Nutzer nutzt jetzt ausschließlich Carsharing.",
         "Wie bewegt er sich mittlerweile fort?"),
        ("u:phoneplan", "Der Nutzer hatte früher einen Business-Tarif.",
         "Der Nutzer nutzt inzwischen einen einfachen Privattarif.",
         "Welchen Handytarif nutzt er zurzeit?"),
        ("u:diet2", "Der Nutzer aß früher täglich Wurst.",
         "Der Nutzer lebt inzwischen vollständig vegan.",
         "Was isst er inzwischen bevorzugt?"),
        ("u:gym", "Der Nutzer trainierte anfangs im Studio Wellenkamm.",
         "Der Nutzer läuft jetzt lieber im Wald.",
         "An welchem Ort trainiert er inzwischen?"),
        ("u:tooltime", "Der Nutzer setzte früher auf das Tool Reiher.",
         "Der Nutzer nutzt mittlerweile das Tool Ammer.",
         "Welche Anwendung setzt er inzwischen ein?"),
        ("u:coffee2", "Der Nutzer trank früher nur Instantkaffee.",
         "Der Nutzer trinkt inzwischen ausschließlich Cappuccino.",
         "Welche Kaffeespezialität trinkt er nun?"),
    ],
    "negative": [
        ("Welche Hunderasse besitzt der Nutzer?",
         "Der Nutzer mag grundsätzlich pflegeleichte Haustiere."),
        ("Wie heißt die Schwester des Nutzers?",
         "Der Nutzer spricht selten über konkrete Verwandte."),
        ("Welchen Bootsführerschein hat der Nutzer?",
         "Der Nutzer findet Technik im Allgemeinen spannend."),
        ("In welcher Theatergruppe spielt der Nutzer?",
         "Der Nutzer geht gern ins Theater, steht aber nie auf der Bühne."),
        ("Wie lautet die Bankkartennummer des Nutzers?",
         "Der Nutzer äußert sich ungern zu Geldangelegenheiten."),
        ("Wann hat der Nutzer in Kairo gewohnt?",
         "Der Nutzer war einmal kurz auf einer Konferenz im Ausland."),
        ("Welches Rennrad-Modell besitzt der Nutzer?",
         "Der Nutzer interessiert sich lose für Ausdauersport."),
        ("Wie lautet die PIN des Nutzers?",
         "Der Nutzer achtet sehr auf Datenschutz."),
        ("Welches Betriebssystem hat der Nutzer geschrieben?",
         "Der Nutzer verwendet beruflich Standardsoftware."),
        ("Wann zieht der Nutzer nach Kanada?",
         "Der Nutzer reist ab und zu gern nach Nordamerika."),
    ],
    "distractor_cities": ["Weißenfeld", "Ostrau", "Lindhorst", "Bergen", "Auental"],
}


def _distractors(pool: dict, rng: random.Random, n: int, tag_prefix: str):
    tmpl = [
        "In {c} eröffnet demnächst ein neues Schwimmbad.",
        "Die Straßenbahn in {c} fährt ab Montag im 10-Minuten-Takt.",
        "Der Wochenmarkt in {c} findet künftig auch freitags statt.",
        "Ein Konzert in {c} lockte {n} Besucher an.",
        "Die Umgehungsstraße bei {c} wird {n} Wochen gesperrt.",
        "In {c} wurde ein altes Rathaus aufwendig saniert.",
    ]
    out = []
    for j in range(n):
        c = rng.choice(pool["distractor_cities"])
        text = tmpl[j % len(tmpl)].format(c=c, n=rng.randint(2, 90))
        out.append({"id": f"{tag_prefix}:dist:{j:04d}", "subject": f"news:{j:04d}",
                    "content": text, "tags": ["distraktor"], "memory_type": "semantic"})
    return out


def build(pool_name: str, *, n_distractors: int = 500) -> dict:
    pool = DEV if pool_name == "dev" else HOLDOUT
    rng = random.Random(pool["seed"])
    pre = pool_name
    facts, queries = [], []
    fid = 0

    def add_fact(subject, content, tags, kind="plain", pair=None):
        nonlocal fid
        f = {"id": f"{pre}:fact:{fid:03d}", "subject": f"{pre}:{subject}",
             "content": content, "tags": tags, "memory_type": "user", "kind": kind}
        if pair is not None:
            f["pair"] = pair
        facts.append(f)
        fid += 1
        return f["id"]

    for i, (subj, fact, q) in enumerate(pool["semantic"]):
        gid = add_fact(subj, fact, ["semantic"])
        queries.append({"id": f"{pre}:q:sem:{i:02d}", "text": q, "gold_id": gid, "qclass": "semantic"})
    for i, (subj, fact, q) in enumerate(pool["exact"]):
        gid = add_fact(subj, fact, ["exact"])
        queries.append({"id": f"{pre}:q:exa:{i:02d}", "text": q, "gold_id": gid, "qclass": "exact"})
    for i, (subj, old, new, q) in enumerate(pool["temporal"]):
        oid = add_fact(subj, old, ["temporal"], kind="temporal_old", pair=i)
        nid = add_fact(subj, new, ["temporal"], kind="temporal_new", pair=i)
        queries.append({"id": f"{pre}:q:tmp:{i:02d}", "text": q, "gold_id": nid,
                        "qclass": "temporal", "stale_id": oid})
    for i, (q, near) in enumerate(pool["negative"]):
        add_fact(f"neg:{i}", near, ["near"])   # aehnlicher Distraktor existiert
        queries.append({"id": f"{pre}:q:neg:{i:02d}", "text": q, "gold_id": None, "qclass": "negative"})

    distractors = _distractors(pool, rng, n_distractors, pre)

    payload = {"facts": facts, "distractors": distractors, "queries": queries}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    from collections import Counter
    payload["meta"] = {"pool": pool_name, "version": VERSION, "seed": pool["seed"],
                       "sha256": sha, "n_facts": len(facts), "n_distractors": len(distractors),
                       "n_queries": len(queries),
                       "classes": dict(Counter(q["qclass"] for q in queries))}
    return payload


if __name__ == "__main__":
    for name in ("dev", "holdout"):
        m = build(name)["meta"]
        print(f"{name:8s} seed={m['seed']} facts={m['n_facts']} dist={m['n_distractors']} "
              f"q={m['n_queries']} classes={m['classes']}")
        print(f"         sha256={m['sha256']}")
