"""
Pobieranie i parsowanie strony https://www.ckziu.jaworzno.pl/zastepstwa/.

Strona po zalogowaniu zawiera:
  1. Akapit "Następujący nauczyciele nie będą prowadzić lekcji: ..."
     - lista nazwisk, część z dopiskiem "(0-5)" oznaczającym zakres
       numerów lekcji, w których dany nauczyciel jest nieobecny.
       Brak dopisku = nieobecność cały dzień.
  2. Tabela zastępstw z kolumnami:
       Klasa (oddział) | Nauczyciel | Lekcja | Przedmiot | Zastępujący | Sala szkolna

     Interesujące przypadki w kolumnie "Zastępujący":
       - "lekcja odwołana"  -> lekcja danego Nauczyciela w tym slocie
                               się nie odbywa.
       - "Imię Nazwisko"    -> ten nauczyciel przejmuje lekcję (zastępstwo),
                               oryginalny Nauczyciel ma w tym slocie
                               "lekcja odwołana" (dorzucamy to automatycznie).
       - "->" / "=>" + sala -> zmiana sali dla tej samej lekcji/klasy,
                               nauczyciel pozostaje ten sam (z planu klasy).
       - "Dyżur" w kolumnie Przedmiot -> wiersz ignorowany (to nie
         regularna lekcja z planu, tylko dyżur na korytarzu/przy szatni).

Funkcje w tym module zwracają dane "surowe" (nazwiska w formie z zastępstw,
tj. "Nazwisko Imię" bez tytułów) - normalizacja do porównań z Firestore
odbywa się w aktualizuj_statusy.py przy użyciu common.normalize_teacher_name.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from common import normalized_text


_NIEOBECNI_MARKER_RE = re.compile(r"Następujący nauczyciele nie będą prowadzić lekcji")

# "Nazwisko Imię(0-5)" lub "Nazwisko Imię(8)" lub samo "Nazwisko Imię"
_NAME_WITH_HOURS_RE = re.compile(r"^(.*?)\((.+)\)$")


def parse_nieobecni(html: str) -> dict[str, str]:
    """
    Zwraca mapę {"Nazwisko Imię": zakres_godzin}.

    zakres_godzin to jeden z:
      - "cały dzień"
      - "N"        (pojedyncza lekcja, np. "8")
      - "N-M"      (zakres, np. "0-5")
    """
    soup = BeautifulSoup(html, "html.parser")

    marker = soup.find(string=_NIEOBECNI_MARKER_RE)
    if marker is None:
        return {}

    # Tekst z nazwiskami znajduje się w div(ach) bezpośrednio po divie
    # zawierającym nagłówek "Następujący nauczyciele...". W praktyce
    # strona dzieli to na kilka <div>, każdy z fragmentem listy nazwisk
    # (przez <br /> w oryginalnym HTML, co BeautifulSoup zwraca jako
    # kolejne stringi/divy).
    header_div = marker.find_parent("div")
    if header_div is None:
        return {}

    container = header_div.find_parent("div")
    if container is None:
        container = header_div

    # Zbierz tekst wszystkich divów-rodzeństwa, które idą po divie
    # z nagłówkiem, aż do napotkania kolejnego znaczącego nagłówka
    # (np. "Sale wyłączone z użytku" albo tabeli).
    raw_parts: list[str] = []
    node = header_div.find_next_sibling()
    while node is not None:
        text = normalized_text(node)
        if not text:
            node = node.find_next_sibling()
            continue
        if "Sale wyłączone" in text or node.find("table") is not None or node.name == "table":
            break
        raw_parts.append(text)
        node = node.find_next_sibling()

    raw = " ".join(raw_parts).strip()
    if not raw:
        return {}

    # Ujednolicenie separatorów: "X i Y" oraz "X i Y(...)" -> przecinek.
    raw = re.sub(r"\s+i\s+", ", ", raw)
    # Niektóre wpisy są rozdzielone tylko spacją między nazwiskiem
    # z nawiasem a kolejnym nazwiskiem - dodaj przecinek po każdym ")".
    raw = re.sub(r"\)\s+(?=[A-ZŁŚŻŹĆŃÓĄĘ])", "), ", raw)

    nieobecni: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip().strip(".")
        if not chunk:
            continue

        match = _NAME_WITH_HOURS_RE.match(chunk)
        if match:
            name = match.group(1).strip()
            hours = match.group(2).strip()
        else:
            name = chunk
            hours = "cały dzień"

        if not name or name.lower().startswith("vacat"):
            # "vacatF" itp. - wakat etatowy, nie konkretna osoba.
            continue

        nieobecni[name] = hours

    return nieobecni


def is_teacher_absent_in_slot(nieobecni: dict[str, str], teacher_name: str, slot: int) -> bool:
    """Czy dany nauczyciel (nazwa już znormalizowana) jest nieobecny w danym slocie."""
    hours = nieobecni.get(teacher_name)
    if hours is None:
        return False
    if hours == "cały dzień":
        return True
    if "-" in hours:
        start_str, _, end_str = hours.partition("-")
        try:
            start, end = int(start_str), int(end_str)
        except ValueError:
            return False
        return start <= slot <= end
    try:
        return int(hours) == slot
    except ValueError:
        return False


_SLOT_RE = re.compile(r"^(\d+)")
# "->" lub "=>" na początku (po ewentualnym strip), opcjonalnie nic po
_ARROW_RE = re.compile(r"^(->|=>)\s*(.*)$")


def _parse_slot(lekcja: str) -> int | None:
    """
    '4'      -> 4
    '2/3'    -> 2   (bieżąca godzina lekcyjna - dyżur na przerwie)
    '10->5'  -> 10  (przesunięcie slotu - rzadki przypadek, traktujemy
                     jak slot źródłowy)
    '0->1'   -> 0
    """
    lekcja = lekcja.strip()
    match = _SLOT_RE.match(lekcja)
    return int(match.group(1)) if match else None


def parse_overrides(html: str) -> list[dict[str, Any]]:
    """
    Zwraca listę nadpisań planu. Każdy element to słownik z polem "action":

      {"action": "odwolane", "teacher": "...", "slot": int, "klasa": "..."}
          -> lekcja tego nauczyciela w tym slocie nie odbywa się.

      {"action": "zastepstwo", "teacher": "...", "slot": int,
       "sala": str | None, "klasa": "...", "przedmiot": "..."}
          -> "teacher" przejmuje lekcję w tym slocie (w podanej sali,
             jeśli wskazana - inaczej bierz salę z planu klasy).
             Oryginalny nauczyciel z kolumny "Nauczyciel" dostaje
             dodatkowo wpis "odwolane" dla tego samego slotu.

      {"action": "sala_zmiana", "klasa": "...", "slot": int,
       "nowa_sala": "..."}
          -> ta sama lekcja (ten sam nauczyciel z planu klasy), ale
             w innej sali.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table")
    if table is None:
        return []

    overrides: list[dict[str, Any]] = []
    rows = table.select("tr")[1:]  # pomiń wiersz nagłówka

    for tr in rows:
        cells = [normalized_text(c) for c in tr.select("td")]
        if len(cells) != 6:
            continue

        klasa, nauczyciel, lekcja, przedmiot, zastepujacy, sala = cells

        # Dyżury nie są regularnymi lekcjami z planu - pomijamy.
        if przedmiot.strip().lower() == "dyżur":
            continue

        slot = _parse_slot(lekcja)
        if slot is None:
            continue

        zastepujacy_norm = zastepujacy.strip()

        # 1. Lekcja odwołana.
        if zastepujacy_norm.lower() == "lekcja odwołana":
            if nauczyciel:
                overrides.append({
                    "action": "odwolane",
                    "teacher": nauczyciel,
                    "slot": slot,
                    "klasa": klasa,
                })
            continue

        # 2. Sama zmiana sali ("->" / "=>" bez nazwiska).
        arrow_match = _ARROW_RE.match(zastepujacy_norm)
        if arrow_match:
            nowa_sala = sala.strip()
            if nowa_sala:
                overrides.append({
                    "action": "sala_zmiana",
                    "klasa": klasa,
                    "slot": slot,
                    "nowa_sala": nowa_sala,
                })
            continue

        # 3. Zastępstwo - konkretny nauczyciel przejmuje lekcję.
        if zastepujacy_norm:
            overrides.append({
                "action": "zastepstwo",
                "teacher": zastepujacy_norm,
                "slot": slot,
                "sala": sala.strip() or None,
                "klasa": klasa,
                "przedmiot": przedmiot,
            })
            if nauczyciel:
                overrides.append({
                    "action": "odwolane",
                    "teacher": nauczyciel,
                    "slot": slot,
                    "klasa": klasa,
                })
            continue

        # 4. Pusta kolumna "Zastępujący" bez "lekcja odwołana" -
        #    zgodnie z konwencją szkoły to też oznacza odwołanie,
        #    ale pojawiło się jako osobny etykietowany przypadek
        #    ("UWAGA – brak informacji w kolumnie Zastępujący oznacza,
        #    że zajęcia są odwołane") w starszych ogłoszeniach.
        if nauczyciel:
            overrides.append({
                "action": "odwolane",
                "teacher": nauczyciel,
                "slot": slot,
                "klasa": klasa,
            })

    return overrides


def group_overrides_by_teacher(overrides: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Grupuje overrides po znormalizowanym nazwisku nauczyciela.

    Wpisy typu "sala_zmiana" (które nie mają pola "teacher") trafiają
    pod specjalny klucz "__sala_zmiany__".
    """
    from common import normalize_teacher_name

    grouped: dict[str, list[dict[str, Any]]] = {}
    for override in overrides:
        if override["action"] == "sala_zmiana":
            grouped.setdefault("__sala_zmiany__", []).append(override)
        else:
            key = normalize_teacher_name(override["teacher"])
            grouped.setdefault(key, []).append(override)
    return grouped


def fetch_zastepstwa_html(session) -> str:
    """
    Pobiera surowy kod HTML ze strony zastępstw przy użyciu uwierzytelnionej sesji.
    """
    url = "https://www.ckziu.jaworzno.pl/zastepstwa/"
    response = session.get(url)
    response.raise_for_status()
    return response.text


__all__ = [
    "parse_nieobecni",
    "parse_overrides",
    "group_overrides_by_teacher",
    "is_teacher_absent_in_slot",
    "fetch_zastepstwa_html",
]
