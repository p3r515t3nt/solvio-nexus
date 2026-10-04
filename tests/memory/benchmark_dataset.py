"""Reproduzierbares SYNTHETISCHES Benchmark-Dataset (STEP 21, PHASE 12).

Nur erfundene Daten — keine echten Nutzer-/Gregor-/Familien-Fakten. Schwerpunkt
Deutsch, mit englischen/cross-language Faellen. Deterministisch (fester Seed);
SHA-256 ueber die kanonische JSON-Serialisierung wird mitgeliefert.

Struktur:
  facts       relevante MemoryRecords (id 'fact:NNN')
  distractors irrelevante MemoryRecords (id 'dist:NNNN')
  queries     {id, text, gold_id, type}  — gold_id verweist auf einen fact

Query-Typen: paraphrase, synonym, indirect, negation, numbers_names, temporal,
short, long, cross_language, reformulation.
"""
from __future__ import annotations

import hashlib
import json
import random

SEED = 20260821

# (subject, fact_content, query_text, query_type, tags)
CURATED: list[tuple[str, str, str, str, list[str]]] = [
    ("user:beverage", "Der Nutzer trinkt morgens am liebsten einen Flat White.",
     "Was für Kaffee mag er früh am Tag?", "paraphrase", ["kaffee", "vorliebe"]),
    ("user:city", "Der Nutzer wohnt seit 2019 in München.",
     "In welcher Stadt lebt er?", "indirect", ["wohnort"]),
    ("user:pet", "Der Nutzer hat einen Golden Retriever namens Rex.",
     "Wie heißt sein Hund?", "numbers_names", ["haustier"]),
    ("user:job", "Der Nutzer arbeitet als Softwarearchitekt bei einer Logistikfirma.",
     "Womit verdient er sein Geld?", "paraphrase", ["beruf"]),
    ("user:allergy", "Der Nutzer verträgt keine Erdnüsse.",
     "Worauf reagiert er allergisch?", "synonym", ["gesundheit"]),
    ("user:sport", "Der Nutzer geht dreimal pro Woche klettern.",
     "Welchen Sport betreibt er regelmäßig?", "paraphrase", ["hobby", "sport"]),
    ("user:car", "Der Nutzer fährt ein blaues Elektroauto.",
     "Was für ein Fahrzeug besitzt er?", "synonym", ["auto"]),
    ("user:music", "Der Nutzer hört gerne Jazz aus den sechziger Jahren.",
     "Welche Musikrichtung gefällt ihm?", "paraphrase", ["musik"]),
    ("user:diet", "Der Nutzer ernährt sich vegetarisch, isst aber Fisch.",
     "Isst er Fleisch?", "negation", ["ernährung"]),
    ("user:language", "Der Nutzer spricht fließend Spanisch.",
     "Which foreign language does he speak well?", "cross_language", ["sprache"]),
    ("person:thomas", "Thomas ist der Steuerberater des Nutzers.",
     "Wer kümmert sich um seine Steuern?", "indirect", ["kontakt"]),
    ("person:anna", "Anna ist die Schwester des Nutzers und wohnt in Hamburg.",
     "Wo lebt seine Schwester?", "indirect", ["familie"]),
    ("project:apollo", "Das Projekt Apollo hat eine Deadline am 30. September.",
     "Wann muss Apollo fertig sein?", "temporal", ["projekt"]),
    ("project:helix", "Im Projekt Helix ist Datenbankmigration die größte Baustelle.",
     "Was ist das Hauptproblem bei Helix?", "paraphrase", ["projekt"]),
    ("user:coffee_machine", "Die Kaffeemaschine des Nutzers ist eine Rocket Appartamento.",
     "Welches Modell ist seine Espressomaschine?", "synonym", ["gerät"]),
    ("user:birthday", "Der Nutzer hat am 14. März Geburtstag.",
     "An welchem Datum wird er geboren gefeiert?", "temporal", ["termin"]),
    ("user:home", "Der Nutzer wohnt in einer Altbauwohnung im dritten Stock ohne Aufzug.",
     "Muss er Treppen steigen um heimzukommen?", "negation", ["wohnung"]),
    ("user:plant", "Der Nutzer besitzt eine Monstera, die viel Licht braucht.",
     "Welche Zimmerpflanze hat er?", "paraphrase", ["pflanze"]),
    ("user:phone", "Der Nutzer nutzt ein Android-Smartphone, kein iPhone.",
     "Benutzt er ein iPhone?", "negation", ["technik"]),
    ("user:travel", "Der Nutzer war letzten Sommer in Portugal wandern.",
     "Wo hat er im vergangenen Sommer Urlaub gemacht?", "temporal", ["reise"]),
    ("person:lena", "Lena ist die Hausärztin des Nutzers.",
     "Who is his general practitioner?", "cross_language", ["kontakt"]),
    ("user:coffee_amount", "Der Nutzer trinkt höchstens zwei Tassen Kaffee am Tag.",
     "Wie viel Koffein nimmt er täglich zu sich?", "reformulation", ["kaffee"]),
    ("user:workhours", "Der Nutzer beginnt seinen Arbeitstag üblicherweise um neun Uhr.",
     "Wann fängt er morgens mit der Arbeit an?", "paraphrase", ["arbeit"]),
    ("user:reading", "Der Nutzer liest am liebsten Science-Fiction-Romane.",
     "Welche Bücher bevorzugt er?", "synonym", ["lesen"]),
    ("user:child", "Die Tochter des Nutzers heißt Mia und geht in die zweite Klasse.",
     "Wie alt ist ungefähr sein Kind in der Schule?", "indirect", ["familie"]),
    ("user:coffee_sugar", "Der Nutzer trinkt seinen Kaffee ohne Zucker.",
     "Süßt er seinen Kaffee?", "negation", ["kaffee"]),
    ("user:instrument", "Der Nutzer spielt seit seiner Kindheit Klavier.",
     "Welches Instrument beherrscht er?", "paraphrase", ["musik", "hobby"]),
    ("project:apollo_lead", "Im Projekt Apollo ist Sarah die technische Leiterin.",
     "Wer führt Apollo technisch?", "synonym", ["projekt", "person"]),
    ("user:coffee_origin", "Der Nutzer bevorzugt Kaffeebohnen aus Äthiopien.",
     "Aus welchem Land mag er seine Bohnen?", "paraphrase", ["kaffee"]),
    ("user:glasses", "Der Nutzer trägt eine Brille zum Lesen.",
     "Braucht er eine Sehhilfe beim Lesen?", "synonym", ["gesundheit"]),
    ("user:commute", "Der Nutzer fährt mit dem Fahrrad zur Arbeit.",
     "Wie kommt er ins Büro?", "paraphrase", ["arbeit", "verkehr"]),
    ("user:temperature", "Der Nutzer mag es zuhause eher kühl, etwa 19 Grad.",
     "Welche Raumtemperatur bevorzugt er?", "numbers_names", ["zuhause"]),
    ("person:markus", "Markus ist der beste Freund des Nutzers aus Studienzeiten.",
     "Wen kennt er am längsten aus dem Studium?", "indirect", ["freund"]),
    ("user:vacation_pref", "Der Nutzer macht lieber Aktivurlaub als Strandurlaub.",
     "Liegt er im Urlaub gerne faul am Strand?", "negation", ["reise"]),
    ("project:helix_stack", "Projekt Helix läuft auf einer PostgreSQL-Datenbank.",
     "Welche Datenbanktechnologie nutzt Helix?", "synonym", ["projekt", "technik"]),
    ("user:breakfast", "Der Nutzer isst zum Frühstück meist Haferbrei mit Beeren.",
     "Was gibt es bei ihm morgens zu essen?", "paraphrase", ["essen"]),
    ("user:tea", "Wenn der Nutzer keinen Kaffee trinkt, dann grünen Tee.",
     "Welches Getränk wählt er als Alternative zu Kaffee?", "reformulation", ["getränk"]),
    ("user:film", "Der Nutzer schaut gerne alte Science-Fiction-Filme von Kubrick.",
     "Welche Filme mag er?", "synonym", ["film"]),
    ("user:workplace_city", "Das Büro des Nutzers liegt in Stuttgart.",
     "In welcher Stadt arbeitet er?", "indirect", ["arbeit"]),
    ("user:coffee_time", "Der Nutzer trinkt nach 16 Uhr keinen Kaffee mehr.",
     "Trinkt er abends noch Kaffee?", "negation", ["kaffee", "zeit"]),
]

_DISTRACTOR_TOPICS = [
    "Das Wetter in {city} war gestern regnerisch und kühl.",
    "Die Buslinie {n} fährt seit Kurzem eine andere Route über {city}.",
    "In {city} eröffnet nächste Woche ein neues Einkaufszentrum.",
    "Der Aktienkurs von {comp} ist um {n} Prozent gefallen.",
    "Ein Rezept für {food} braucht mindestens {n} Minuten Backzeit.",
    "Das Fußballspiel in {city} endete {n} zu eins.",
    "Die Fähre nach {city} verkehrt nur im Sommer.",
    "Ein Marathon in {city} zieht jedes Jahr {n} Tausend Läufer an.",
    "Die Bibliothek in {city} hat sonntags geschlossen.",
    "Ein Vulkan in der Nähe von {city} war vor {n} Jahren aktiv.",
]
_CITIES = ["Bremen", "Köln", "Leipzig", "Dresden", "Kiel", "Mainz", "Erfurt", "Ulm",
           "Trier", "Passau", "Aachen", "Fulda", "Gera", "Hof", "Jena", "Lübeck"]
_COMPS = ["Nordwind AG", "Blaustein GmbH", "Kestrel Corp", "Marnovia Ltd", "Halvern Inc"]
_FOODS = ["Apfelkuchen", "Sauerteigbrot", "Zimtschnecken", "Käsekuchen", "Focaccia"]


def _templated_facts(rng: random.Random, start: int, n: int):
    """Zusaetzliche relevante Fakten + gematchte Paraphrase-Queries (Volumen)."""
    subjects = ["gadget", "buch", "reiseziel", "kochen", "seriennr", "termin2", "kollege"]
    out = []
    for i in range(n):
        idx = start + i
        subj = f"user:{subjects[i % len(subjects)]}:{idx}"
        color = rng.choice(["rote", "grüne", "schwarze", "silberne"])
        thing = rng.choice(["Notiztasche", "Trinkflasche", "Kopfhörer", "Regenjacke", "Tastatur"])
        num = rng.randint(2, 40)
        fact = f"Der Nutzer besitzt eine {color} {thing} mit der Nummer {num}."
        query = f"Welche {thing} gehört ihm?"
        out.append((subj, fact, query, "paraphrase", ["besitz"]))
    return out


def build_dataset(n_relevant: int = 100, n_distractors: int = 1000) -> dict:
    rng = random.Random(SEED)
    pairs = list(CURATED)
    if len(pairs) < n_relevant:
        pairs += _templated_facts(rng, len(pairs), n_relevant - len(pairs))
    pairs = pairs[:n_relevant]

    facts, queries = [], []
    for i, (subj, fact, query, qtype, tags) in enumerate(pairs):
        fid = f"fact:{i:03d}"
        facts.append({"id": fid, "subject": subj, "content": fact,
                      "tags": tags, "memory_type": "user"})
        queries.append({"id": f"q:{i:03d}", "text": query, "gold_id": fid, "type": qtype})

    distractors = []
    for j in range(n_distractors):
        tmpl = _DISTRACTOR_TOPICS[j % len(_DISTRACTOR_TOPICS)]
        text = tmpl.format(city=rng.choice(_CITIES), n=rng.randint(1, 90),
                           comp=rng.choice(_COMPS), food=rng.choice(_FOODS))
        distractors.append({"id": f"dist:{j:04d}", "subject": f"news:{j:04d}",
                            "content": text, "tags": ["distraktor"], "memory_type": "semantic"})

    payload = {"facts": facts, "distractors": distractors, "queries": queries}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["meta"] = {"seed": SEED, "sha256": sha, "n_facts": len(facts),
                       "n_distractors": len(distractors), "n_queries": len(queries)}
    return payload


if __name__ == "__main__":
    ds = build_dataset()
    m = ds["meta"]
    print(f"facts={m['n_facts']} distractors={m['n_distractors']} queries={m['n_queries']}")
    print(f"seed={m['seed']} sha256={m['sha256']}")
    from collections import Counter
    print("query types:", dict(Counter(q["type"] for q in ds["queries"])))
