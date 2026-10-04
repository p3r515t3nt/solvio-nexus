"""STEP 21.2C — M4 Embedding-Validierung (Qwen3-Embedding-0.6B).

Identisch auf Windows (CPU-Referenz) und Mac (MPS) ausfuehren:
  python m4_embed_bench.py perf     # cold load, warm p50/p95, batch, RSS
  python m4_embed_bench.py parity   # feste Rankings (JSON) fuer Cross-Machine-Vergleich
  python m4_embed_bench.py worker    # laedt Modell, embeddet, exit (fuer Unload-Test)

Nur SYNTHETISCHE Daten (fest, deterministisch, keine echten Memories). Gleiche
Parameter wie der Repo-Provider: Qwen3-Embedding-0.6B, dim 1024, L2-normalisiert,
Query-Prompt "query".
"""
from __future__ import annotations

import json
import sys
import time

MODEL = "Qwen/Qwen3-Embedding-0.6B"

# 50 feste synthetische Dokumente (deutsch, erfunden).
DOCS = [
    "Der Nutzer trinkt morgens am liebsten einen doppelten Espresso.",              # 0
    "Der Nutzer pendelt werktags mit der Straßenbahn ins Büro.",                    # 1
    "Am Wochenende geht der Nutzer bevorzugt bergsteigen.",                         # 2
    "Der Nutzer meidet scharfe Speisen grundsätzlich.",                            # 3
    "Der Nutzer hört begeistert barocke Orchestermusik.",                          # 4
    "Der Nutzer liest am liebsten historische Romane.",                            # 5
    "Wegen des Rückens geht der Nutzer regelmäßig schwimmen.",                      # 6
    "Der Nutzer spricht verhandlungssicher Italienisch.",                          # 7
    "Der Nutzer fühlt sich bei kühlem Nebelwetter am wohlsten.",                    # 8
    "Beruflich entwirft der Nutzer Brückenkonstruktionen.",                        # 9
    "Der Nutzer hält zwei Wellensittiche als Haustiere.",                          # 10
    "Der Nutzer geht meist erst nach Mitternacht schlafen.",                       # 11
    "Die Rechnung INV-4471 über 2480 Euro ist noch offen.",                        # 12
    "Frau Quenzer betreut den Vertrag mit der Nummer V-88.",                       # 13
    "Der Server Nordpol-7 trägt die Seriennummer SNX-30291.",                      # 14
    "Das Team trifft sich in Raum B214 im Gebäude Habichtsweg.",                    # 15
    "Der Rückflug ist mit LH417 am zwölften November gebucht.",                     # 16
    "Charge LOT4501 wurde am dritten Mai freigegeben.",                            # 17
    "Der Nutzer wohnt inzwischen in Freiburg.",                                    # 18
    "Der Nutzer arbeitet mittlerweile als Teamleiter.",                            # 19
    "In Nordkirchen eröffnet demnächst ein neues Schwimmbad.",                     # 20
    "Der Wochenmarkt in Talberg findet künftig auch freitags statt.",              # 21
    "Die Umgehungsstraße bei Seewinkel wird sechs Wochen gesperrt.",               # 22
    "Ein Konzert in Moorbach lockte dreitausend Besucher an.",                     # 23
    "Der Nutzer sammelt alte Briefmarken aus der Kaiserzeit.",                     # 24
    "Der Nutzer bevorzugt Tee gegenüber Kaffee am Nachmittag.",                    # 25
    "Der Nutzer fährt ein Elektroauto der Kompaktklasse.",                         # 26
    "Der Nutzer baut im Garten Tomaten und Kräuter an.",                           # 27
    "Der Nutzer trägt beim Radfahren stets einen Helm.",                           # 28
    "Der Nutzer plant eine Reise nach Neuseeland im Winter.",                      # 29
    "Der Drucker im dritten Stock ist erneut ausgefallen.",                        # 30
    "Die Kantine bietet freitags immer frischen Fisch an.",                        # 31
    "Der Aufzug im Westflügel wird nächste Woche gewartet.",                       # 32
    "Das Passwort für das Gäste-WLAN wechselt jeden Monat.",                       # 33
    "Der Nutzer trinkt abends gerne einen Kräutertee.",                            # 34
    "Der Nutzer hat eine Vorliebe für dunkle Schokolade.",                         # 35
    "Der Nutzer joggt dreimal pro Woche im nahen Park.",                           # 36
    "Der Nutzer nutzt beruflich vor allem Tabellenkalkulation.",                   # 37
    "Der Nutzer besitzt eine kleine Sammlung alter Uhren.",                        # 38
    "Der Nutzer bevorzugt beim Essen regionale Zutaten.",                          # 39
    "Die Lieferung der Ersatzteile verzögert sich um drei Tage.",                  # 40
    "Das jährliche Sommerfest findet dieses Jahr im Juli statt.",                  # 41
    "Der neue Kollege beginnt seine Arbeit am ersten Montag.",                     # 42
    "Die Software-Aktualisierung ist für das Wochenende geplant.",                 # 43
    "Der Nutzer spielt in seiner Freizeit gerne Schach.",                          # 44
    "Der Nutzer trinkt selten Alkohol und bevorzugt Wasser.",                      # 45
    "Der Nutzer interessiert sich stark für Astronomie.",                          # 46
    "Der Nutzer hört beim Kochen oft Jazzmusik.",                                  # 47
    "Der Nutzer bevorzugt Nachtzüge gegenüber Kurzstreckenflügen.",                # 48
    "Der Nutzer notiert Ideen am liebsten in einem Papierheft.",                   # 49
]

# 20 Queries (Paraphrase/indirekt/exakt) -> gold = Dokument-Index.
QUERIES = [
    ("Welches Heißgetränk mag er früh am Tag?", 0),
    ("Wie kommt er zur Arbeit?", 1),
    ("Was macht er samstags am liebsten?", 2),
    ("Verträgt er würziges Essen?", 3),
    ("Welche Art von Musik begeistert ihn?", 4),
    ("Welche Bücher liest er bevorzugt?", 5),
    ("Warum geht er oft ins Schwimmbad?", 6),
    ("Welche Fremdsprache beherrscht er gut?", 7),
    ("Bei welchem Wetter fühlt er sich wohl?", 8),
    ("Was ist sein berufliches Fachgebiet?", 9),
    ("Welche Tiere hält er zuhause?", 10),
    ("Ist er ein Frühschläfer?", 11),
    ("Welcher Betrag steht auf Rechnung INV-4471?", 12),
    ("Wer betreut Vertrag V-88?", 13),
    ("Wie lautet die Seriennummer von Server Nordpol-7?", 14),
    ("In welchem Raum trifft sich das Team?", 15),
    ("Mit welchem Flug ist der Rückflug gebucht?", 16),
    ("Wann wurde Charge LOT4501 freigegeben?", 17),
    ("Wo wohnt der Nutzer aktuell?", 18),
    ("Welche Position hat er derzeit?", 19),
]


def rss_mb() -> float:
    # macOS: ru_maxrss in Bytes; Linux: in Kilobytes. Windows: resource fehlt -> -1.
    try:
        import resource
    except ImportError:
        return -1.0
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / (1024 * 1024) if sys.platform == "darwin" else v / 1024


def load_model():
    import torch
    from sentence_transformers import SentenceTransformer
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    t0 = time.perf_counter()
    model = SentenceTransformer(MODEL, device=dev)
    return model, dev, time.perf_counter() - t0


def embed(model, texts, is_query):
    kw = dict(normalize_embeddings=True, convert_to_numpy=True, batch_size=32)
    if is_query:
        kw["prompt_name"] = "query"
    return model.encode(texts, **kw)


def cmd_perf():
    import numpy as np
    model, dev, load_s = load_model()
    print(f"device={dev}")
    print(f"model_load_s={load_s:.2f}")
    t = time.perf_counter()
    v = embed(model, [QUERIES[0][0]], True)
    print(f"first_query_ms={(time.perf_counter()-t)*1000:.1f}")
    print(f"dim={len(v[0])} first_norm={float(np.linalg.norm(v[0])):.4f}")
    lat = []
    for i in range(30):
        q = QUERIES[i % len(QUERIES)][0]
        t = time.perf_counter()
        embed(model, [q], True)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    print(f"warm_single_p50_ms={lat[len(lat)//2]:.1f}")
    print(f"warm_single_p95_ms={lat[min(int(len(lat)*0.95), len(lat)-1)]:.1f}")
    for bs in (8, 32):
        texts = [DOCS[i % len(DOCS)] for i in range(bs)]
        t = time.perf_counter()
        embed(model, texts, False)
        dt = time.perf_counter() - t
        print(f"batch{bs}_ms={dt*1000:.1f} docs_per_s={bs/dt:.1f}")
    print(f"peak_rss_mb={rss_mb():.0f}")


def cmd_parity():
    import numpy as np
    model, dev, _ = load_model()
    D = np.asarray(embed(model, DOCS, False))
    Q = np.asarray(embed(model, [q for q, _ in QUERIES], True))
    r1 = r3 = 0
    rankings = []
    for i, (q, gold) in enumerate(QUERIES):
        order = [int(x) for x in np.argsort(-(D @ Q[i]))[:3]]
        rankings.append(order)
        r1 += int(order[0] == gold)
        r3 += int(gold in order)
    print(json.dumps({
        "device": dev, "dim": int(D.shape[1]),
        "doc_norm_mean": round(float(np.mean(np.linalg.norm(D, axis=1))), 4),
        "recall1": round(r1 / len(QUERIES), 3), "recall3": round(r3 / len(QUERIES), 3),
        "rankings": rankings,
    }))


def cmd_worker():
    model, dev, load_s = load_model()
    embed(model, [q for q, _ in QUERIES], True)
    print(f"worker device={dev} load_s={load_s:.2f} rss_mb={rss_mb():.0f}")


if __name__ == "__main__":
    {"perf": cmd_perf, "parity": cmd_parity, "worker": cmd_worker}[sys.argv[1]]()
