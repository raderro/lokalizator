"""
Główny skrypt aktualizujący statusy nauczycieli w Firestore na podstawie:
  - planu lekcji (plan.ckziu.jaworzno.pl),
  - bieżących zastępstw (https://www.ckziu.jaworzno.pl/zastepstwa/),
  - listy nieobecnych nauczycieli (też ze strony zastępstw).

Uruchamiany przez GitHub Actions co kilka minut. Sam skrypt dodatkowo
sprawdza okno czasowe 6:00-18:30 (Europe/Warsaw, pon-pt) i kończy się
natychmiast poza tym oknem - dzięki temu cron w workflow może być
ustawiony szerzej (np. cały dzień UTC) bez ryzyka, a faktyczna praca
i tak wykonuje się tylko w godzinach szkolnych.

Zasady aktualizacji:
  - Status nadpisywany jest TYLKO jeśli ostatnia zmiana nie była "manual"
    w ciągu ostatnich MANUAL_OVERRIDE_GRACE_MINUTES minut (żeby nie
    "gryzło się" z ręcznymi zmianami w aplikacji).
  - Nauczyciel nieobecny w danym slocie (wg /zastepstwa/) -> status NIE
    jest nadpisywany (zostaje to, co ustawili użytkownicy / poprzedni stan).
  - Lekcja odwołana w danym slocie -> status NIE jest nadpisywany.
  - Zastępstwo (ktoś przejmuje lekcję) -> ZASTĘPUJĄCY nauczyciel dostaje
    status "W sali / pomieszczeniu" w odpowiedniej sali.
  - Brak override, normalna lekcja z planu -> nauczyciel dostaje status
    "W sali / pomieszczeniu" w sali z planu (z uwzględnieniem ewentualnej
    zmiany sali z zastępstw).
  - Przerwa / okienko / brak lekcji w danym slocie -> status NIE jest
    nadpisywany (użytkownik decyduje sam, gdzie przebywa).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import firebase_admin
import requests
from firebase_admin import credentials, firestore

from common import UpstreamError, fetch_zastepstwa_html, get_zastepstwa_session, normalize_teacher_name
from plan_source import get_plan_session, get_schedule
from zastepstwa_source import (
    group_overrides_by_teacher,
    is_teacher_absent_in_slot,
    parse_nieobecni,
    parse_overrides,
)


TZ = ZoneInfo("Europe/Warsaw")
WORK_START = dtime(6, 0)
WORK_END = dtime(18, 30)

# Skrócone nazwy dni z planu (.md-size) -> indeks 0=pon .. 4=pt
DAY_SHORT_NAMES = {
    0: ["Pon", "Pn"],
    1: ["Wt"],
    2: ["Śr", "Sr"],
    3: ["Czw", "Cz"],
    4: ["Pt"],
}

MAPPING_PATH = os.path.join(os.path.dirname(__file__), "nauczyciele_mapping.json")

# Ile minut po ręcznej zmianie statusu auto-aktualizacja "odpuszcza" tego
# nauczyciela, żeby nie nadpisywać świeżej decyzji użytkownika.
MANUAL_OVERRIDE_GRACE_MINUTES = 25

STATUS_W_SALI = "W sali / pomieszczeniu"


# ---------------------------------------------------------------------------
# Inicjalizacja Firebase
# ---------------------------------------------------------------------------

def init_firestore():
    raw_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY")
    if not raw_key:
        print("Brak FIREBASE_SERVICE_ACCOUNT_KEY w zmiennych środowiskowych", file=sys.stderr)
        sys.exit(1)

    cred_dict = json.loads(raw_key)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------------------------------------------------------------------
# Okno czasowe
# ---------------------------------------------------------------------------

def is_within_active_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # sobota=5, niedziela=6
        return False
    current_time = now.timetz().replace(tzinfo=None)
    return WORK_START <= current_time <= WORK_END


# ---------------------------------------------------------------------------
# Mapowanie nauczyciel -> plan_teacher_id
# ---------------------------------------------------------------------------

def load_teacher_mapping() -> dict[str, str]:
    if not os.path.exists(MAPPING_PATH):
        print(f"UWAGA: nie znaleziono {MAPPING_PATH} - uruchom build_teacher_mapping.py", file=sys.stderr)
        return {}
    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Wyznaczanie aktualnego slotu lekcyjnego na podstawie planu danego dnia
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str) -> dtime | None:
    value = value.strip()
    for fmt in ("%H:%M", "%H.%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return dtime(parsed.hour, parsed.minute)
        except ValueError:
            continue
    return None


def find_current_slot(day_schedule: dict, now: datetime) -> int | None:
    """
    Zwraca numer slotu (slot["slot"]), w którym aktualnie jesteśmy, na
    podstawie godzin start/end zapisanych w slot["time"]. Zwraca None,
    jeśli aktualny czas nie wpada w żaden slot lekcyjny (przerwa,
    przed/po zajęciach) albo dane o czasie są niedostępne.
    """
    current_time = now.timetz().replace(tzinfo=None)

    for slot in day_schedule.get("slots", []):
        time_info = slot.get("time") or {}
        start_str = time_info.get("start")
        end_str = time_info.get("end")
        if not start_str or not end_str:
            continue

        start = _parse_hhmm(start_str)
        end = _parse_hhmm(end_str)
        if start is None or end is None:
            continue

        if start <= current_time <= end:
            return slot["slot"]

    return None


def find_day_schedule(plan: dict, now: datetime) -> dict | None:
    weekday = now.weekday()  # 0=pon .. 6=niedz
    candidates = DAY_SHORT_NAMES.get(weekday, [])
    if not candidates:
        return None

    for day in plan.get("days", []):
        short_name = day.get("short_name", "")
        if any(short_name.startswith(candidate) for candidate in candidates):
            return day

    return None


# ---------------------------------------------------------------------------
# Wyznaczanie statusu dla pojedynczego nauczyciela
# ---------------------------------------------------------------------------

def determine_status(
    teacher_name_norm: str,
    day_schedule: dict,
    slot_idx: int,
    nieobecni: dict[str, str],
    overrides_by_teacher: dict[str, list[dict]],
) -> dict[str, str] | None:
    """
    Zwraca {"location": ..., "status": ...} albo None, jeśli status nie
    powinien być nadpisany w tym przebiegu.
    """
    # 1. Nauczyciel nieobecny w tym slocie -> nic nie zmieniamy.
    if is_teacher_absent_in_slot(nieobecni, teacher_name_norm, slot_idx):
        return None

    own_overrides = overrides_by_teacher.get(teacher_name_norm, [])

    # 2. Czy ten nauczyciel przejmuje (zastępuje) lekcję w tym slocie?
    zastepstwa = [o for o in own_overrides if o["action"] == "zastepstwo" and o["slot"] == slot_idx]
    if zastepstwa:
        override = zastepstwa[0]
        sala = override.get("sala")
        if sala:
            return {"location": _format_location(sala), "status": STATUS_W_SALI}
        # Brak wskazanej sali w zastępstwie - nie zgadujemy, pomijamy.
        return None

    # 3. Czy własna lekcja tego nauczyciela w tym slocie jest odwołana?
    odwolania = [o for o in own_overrides if o["action"] == "odwolane" and o["slot"] == slot_idx]
    if odwolania:
        return None

    # 4. Standardowo - bierzemy lekcję z planu.
    slot = next((s for s in day_schedule.get("slots", []) if s["slot"] == slot_idx), None)
    if slot is None or slot.get("lesson_count", 0) == 0:
        return None

    lesson = next((l for l in slot["lessons"] if l.get("type") == "lesson"), None)
    if lesson is None:
        return None

    sala = lesson.get("classroom_full") or lesson.get("classroom_short")
    if not sala:
        return None

    # 5. Czy jest zmiana sali dla tej klasy/grupy w tym slocie?
    sala_zmiany = overrides_by_teacher.get("__sala_zmiany__", [])
    group_full = lesson.get("group_full") or lesson.get("group")
    zmiana = next(
        (o for o in sala_zmiany if o["slot"] == slot_idx and _klasa_matches(o["klasa"], group_full)),
        None,
    )
    if zmiana:
        sala = zmiana["nowa_sala"]

    return {"location": _format_location(sala), "status": STATUS_W_SALI}


def _klasa_matches(klasa_zastepstwa: str, group_full: str | None) -> bool:
    """
    Porównanie nazw klas/grup między zastępstwami a planem - oba źródła
    mogą formatować nazwy nieco inaczej (spacje, wielkość liter), więc
    porównujemy po znormalizowanym tekście. Dopuszczamy też dopasowanie
    częściowe (zastępstwa bywają bardziej szczegółowe, np. "1. Grupa").
    """
    if not group_full:
        return False
    a = " ".join(klasa_zastepstwa.split()).casefold()
    b = " ".join(group_full.split()).casefold()
    return a == b or a in b or b in a


def _format_location(sala: str) -> str:
    sala = sala.strip()
    if not sala:
        return sala
    if sala.lower().startswith("sala"):
        return sala
    return f"Sala {sala}"


# ---------------------------------------------------------------------------
# Główna pętla
# ---------------------------------------------------------------------------

def diagnose_connectivity() -> None:
    """
    Szybki test łączności z oboma hostami (osobny serwer dla planu i dla
    strony WWW/zastępstw - różne hostingi mogą mieć różne firewalle).
    Wynik trafia do logów, niezależnie od tego, czy faktyczne zapytania
    się powiodą - pomaga to odróżnić "host nieosiągalny z tego runnera"
    od "logowanie się nie powiodło".
    """
    import requests as _requests

    for label, url in (
        ("plan.ckziu.jaworzno.pl", "https://plan.ckziu.jaworzno.pl"),
        ("www.ckziu.jaworzno.pl", "https://www.ckziu.jaworzno.pl"),
    ):
        try:
            resp = _requests.head(url, timeout=10)
            print(f"[diagnoza] {label}: OK (status {resp.status_code})")
        except _requests.exceptions.RequestException as exc:
            print(f"[diagnoza] {label}: NIEOSIĄGALNY z tego runnera ({type(exc).__name__}: {exc})", file=sys.stderr)


def main() -> None:
    now = datetime.now(TZ)

    if not is_within_active_window(now):
        print(f"[{now.isoformat()}] Poza oknem aktywności (6:00-18:30, pon-pt) - kończę.")
        return

    diagnose_connectivity()

    teacher_mapping = load_teacher_mapping()
    if not teacher_mapping:
        print("Brak mapowania nauczycieli - kończę.", file=sys.stderr)
        return

    # --- Pobierz i sparsuj zastępstwa ---
    try:
        zastepstwa_session = get_zastepstwa_session()
        zastepstwa_html = fetch_zastepstwa_html(zastepstwa_session)
    except UpstreamError as exc:
        print(f"Błąd logowania/pobierania zastępstw: {exc}", file=sys.stderr)
        return
    except requests.exceptions.RequestException as exc:
        print(
            f"Błąd sieciowy przy łączeniu z www.ckziu.jaworzno.pl "
            f"({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        print(
            "Host może być nieosiągalny z zakresu IP tego runnera "
            "(np. firewall hostingu blokuje ruch z chmur typu Azure/GitHub Actions).",
            file=sys.stderr,
        )
        return

    nieobecni_raw = parse_nieobecni(zastepstwa_html)
    nieobecni = {normalize_teacher_name(name): hours for name, hours in nieobecni_raw.items()}

    overrides = parse_overrides(zastepstwa_html)
    overrides_by_teacher = group_overrides_by_teacher(overrides)

    print(f"[{now.isoformat()}] Nieobecni: {len(nieobecni)}, overrides: {len(overrides)}")

    # --- Sesja do planu ---
    try:
        plan_session = get_plan_session()
    except UpstreamError as exc:
        print(f"Błąd logowania do planu: {exc}", file=sys.stderr)
        return
    except requests.exceptions.RequestException as exc:
        print(
            f"Błąd sieciowy przy łączeniu z plan.ckziu.jaworzno.pl "
            f"({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return

    db = init_firestore()
    nauczyciele_ref = db.collection("nauczyciele")

    updated_count = 0
    skipped_count = 0
    error_count = 0

    for doc in nauczyciele_ref.stream():
        dane = doc.to_dict() or {}
        raw_name = dane.get("name", "")
        teacher_name_norm = normalize_teacher_name(raw_name)
        if not teacher_name_norm:
            continue

        teacher_id = teacher_mapping.get(teacher_name_norm)
        if not teacher_id:
            # Brak mapowania - nie ingerujemy w status tego nauczyciela.
            skipped_count += 1
            continue

        # Nie nadpisuj świeżej ręcznej zmiany.
        if dane.get("status_source") == "manual":
            last_updated = dane.get("lastUpdated")
            if last_updated is not None:
                last_updated_dt = last_updated.astimezone(TZ) if hasattr(last_updated, "astimezone") else None
                if last_updated_dt is not None:
                    delta_minutes = (now - last_updated_dt).total_seconds() / 60
                    if delta_minutes < MANUAL_OVERRIDE_GRACE_MINUTES:
                        skipped_count += 1
                        continue

        try:
            plan = get_schedule(plan_session, "teacher", teacher_id)
        except UpstreamError as exc:
            print(f"  {raw_name}: błąd pobierania planu ({exc})", file=sys.stderr)
            error_count += 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  {raw_name}: nieoczekiwany błąd planu ({exc})", file=sys.stderr)
            error_count += 1
            continue

        day_schedule = find_day_schedule(plan, now)
        if day_schedule is None:
            skipped_count += 1
            continue

        slot_idx = find_current_slot(day_schedule, now)
        if slot_idx is None:
            skipped_count += 1
            continue

        wynik = determine_status(
            teacher_name_norm=teacher_name_norm,
            day_schedule=day_schedule,
            slot_idx=slot_idx,
            nieobecni=nieobecni,
            overrides_by_teacher=overrides_by_teacher,
        )
        if wynik is None:
            skipped_count += 1
            continue

        if dane.get("location") == wynik["location"] and dane.get("status") == wynik["status"]:
            skipped_count += 1
            continue

        doc.reference.update({
            "location": wynik["location"],
            "status": wynik["status"],
            "status_source": "auto",
            "lastUpdated": firestore.SERVER_TIMESTAMP,
            "historia": firestore.ArrayUnion([{
                "status": wynik["status"],
                "location": wynik["location"],
                "timestamp": firestore.SERVER_TIMESTAMP,
                "source": "auto",
            }]),
        })
        updated_count += 1
        print(f"  {raw_name}: -> {wynik['status']} @ {wynik['location']} (slot {slot_idx})")

    print(
        f"[{now.isoformat()}] Zakończono. "
        f"Zaktualizowano: {updated_count}, pominięto: {skipped_count}, błędy: {error_count}."
    )


if __name__ == "__main__":
    main()