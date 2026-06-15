"""
Budowanie mapowania "Nazwisko Imię" -> teacher_id (plan.ckziu.jaworzno.pl).

Uruchamiane okresowo (ręcznie / przez workflow build_mapping.yml), NIE przy
każdej aktualizacji statusów - lista nauczycieli zmienia się rzadko
(początek roku szkolnego, zmiany kadrowe).

Strategia:
  1. Spróbuj wyciągnąć listę nauczycieli ze strony głównej planu - typowy
     układ aSc Plan Lekcji to lista linków <a href="?teacherid=N">Nazwisko
     Imię</a> w sekcji "Nauczyciele".
  2. Jeśli to się nie powiedzie (inny układ strony / brak takiej listy),
     przejdź do trybu brute-force: dla teacherid w zadanym zakresie pobierz
     plan i odczytaj nazwę z .class-name. Zakres jest konfigurowalny
     zmiennymi środowiskowymi BRUTE_FORCE_MIN / BRUTE_FORCE_MAX.

Wynik zapisywany jest do scripts/nauczyciele_mapping.json w formacie:

{
  "Kowalczyk Mirosław": "42",
  "Stawski Jakub": "17",
  ...
}

Klucze są już znormalizowane (common.normalize_teacher_name), więc
aktualizuj_statusy.py może bezpośrednio porównywać po nazwie z Firestore
(po jej normalizacji).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from bs4 import BeautifulSoup

from common import UpstreamError, normalize_teacher_name
from plan_source import fetch_homepage_html, get_plan_session, get_schedule


MAPPING_PATH = os.path.join(os.path.dirname(__file__), "nauczyciele_mapping.json")

# Zakres dla trybu brute-force - większość systemów aSc ma ID nauczycieli
# w niewielkim zakresie (kilka-kilkadziesiąt-kilkaset).
BRUTE_FORCE_MIN = int(os.environ.get("BRUTE_FORCE_MIN", "1"))
BRUTE_FORCE_MAX = int(os.environ.get("BRUTE_FORCE_MAX", "150"))
BRUTE_FORCE_SLEEP_SECONDS = float(os.environ.get("BRUTE_FORCE_SLEEP_SECONDS", "0.3"))


def _try_homepage_list(session) -> dict[str, str]:
    """
    Próbuje wyciągnąć mapowanie z linków `?teacherid=N` na stronie głównej.
    Zwraca pusty dict, jeśli nic nie znaleziono (nie traktujemy tego jako
    błąd - po prostu przechodzimy do brute-force).
    """
    try:
        html = fetch_homepage_html(session)
    except UpstreamError as exc:
        print(f"[mapping] Nie udało się pobrać strony głównej planu: {exc}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(html, "html.parser")
    mapping: dict[str, str] = {}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.search(r"teacherid=(\d+)", href)
        if not match:
            continue
        teacher_id = match.group(1)
        name = " ".join(link.get_text(" ", strip=True).split())
        if not name:
            continue
        mapping[normalize_teacher_name(name)] = teacher_id

    return mapping


def _brute_force(session) -> dict[str, str]:
    """
    Iteruje po zakresie ID i odpytuje plan każdego nauczyciela, odczytując
    jego nazwę z .class-name. Wolniejsze, ale działa niezależnie od
    układu strony głównej.
    """
    mapping: dict[str, str] = {}

    for teacher_id in range(BRUTE_FORCE_MIN, BRUTE_FORCE_MAX + 1):
        try:
            schedule = get_schedule(session, "teacher", str(teacher_id))
        except UpstreamError:
            continue
        except Exception as exc:  # noqa: BLE001 - chcemy kontynuować mimo błędów sieciowych
            print(f"[mapping] teacherid={teacher_id}: błąd {exc}", file=sys.stderr)
            time.sleep(BRUTE_FORCE_SLEEP_SECONDS)
            continue

        name = schedule.get("name", "").strip()
        if not name:
            time.sleep(BRUTE_FORCE_SLEEP_SECONDS)
            continue

        mapping[normalize_teacher_name(name)] = str(teacher_id)
        print(f"[mapping] teacherid={teacher_id} -> {name!r}")
        time.sleep(BRUTE_FORCE_SLEEP_SECONDS)

    return mapping


def build_mapping() -> dict[str, str]:
    session = get_plan_session()

    mapping = _try_homepage_list(session)
    if mapping:
        print(f"[mapping] Znaleziono {len(mapping)} nauczycieli na stronie głównej planu.")
        return mapping

    print("[mapping] Strona główna nie dała wyniku - przechodzę do trybu brute-force "
          f"(teacherid {BRUTE_FORCE_MIN}..{BRUTE_FORCE_MAX}).")
    mapping = _brute_force(session)
    print(f"[mapping] Brute-force znalazł {len(mapping)} nauczycieli.")
    return mapping


def main() -> None:
    mapping = build_mapping()

    if not mapping:
        print("[mapping] UWAGA: nie znaleziono żadnych nauczycieli - "
              "nie nadpisuję istniejącego pliku.", file=sys.stderr)
        sys.exit(1)

    # Jeśli plik już istnieje, scal ze starym mapowaniem (nowe wpisy
    # nadpisują stare dla tych samych nazwisk, ale nie usuwamy wpisów,
    # których akurat nie udało się ponownie znaleźć - np. nauczyciel
    # akurat nie ma żadnych lekcji i jego strona wygląda inaczej).
    existing: dict[str, str] = {}
    if os.path.exists(MAPPING_PATH):
        with open(MAPPING_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)

    merged = {**existing, **mapping}

    with open(MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print(f"[mapping] Zapisano {len(merged)} wpisów do {MAPPING_PATH}")


if __name__ == "__main__":
    main()
