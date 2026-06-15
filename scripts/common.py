"""
Wspólne narzędzia używane przez moduły plan_source.py i zastepstwa_source.py.

- Zarządzanie sesjami HTTP (logowanie do dwóch różnych serwisów,
  każdy z własnym ciasteczkiem/sesją).
- Normalizacja nazwisk nauczycieli (usuwanie tytułów typu "mgr.", "mgr inż.",
  "dr" itp.), żeby dane z Firestore, planu i zastępstw dało się porównywać.
- Drobne helpery do BeautifulSoup.
"""

from __future__ import annotations

import os
import re

import requests


# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

SCHOOL_PASSWORD = os.environ.get("SCHOOL_PASSWORD", "")

PLAN_BASE_URL = "https://plan.ckziu.jaworzno.pl"
PLAN_LOGIN_URL = f"{PLAN_BASE_URL}/login"

ZASTEPSTWA_URL = "https://www.ckziu.jaworzno.pl/zastepstwa/"

REQUEST_TIMEOUT = 20


class UpstreamError(Exception):
    """Błąd komunikacji z serwisem zewnętrznym (logowanie / parsowanie)."""


# ---------------------------------------------------------------------------
# Sesje HTTP
# ---------------------------------------------------------------------------

def get_plan_session() -> requests.Session:
    """
    Zaloguj się do plan.ckziu.jaworzno.pl i zwróć aktywną sesję.

    Każde wywołanie tworzy nową sesję — skrypt działa jako jednorazowy
    proces (GitHub Actions), więc nie ma potrzeby cache'owania między
    wywołaniami procesu. W ramach jednego przebiegu warto trzymać tę
    sesję w zmiennej i przekazywać dalej.
    """
    if not SCHOOL_PASSWORD:
        raise UpstreamError("Brak SCHOOL_PASSWORD w zmiennych środowiskowych")

    session = requests.Session()
    response = session.post(
        PLAN_LOGIN_URL,
        data={"password": SCHOOL_PASSWORD},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    if "Podaj hasło" in response.text or "password" in response.text.lower() and "login" in response.url.lower():
        # Dodatkowe zabezpieczenie: jeśli po POST nadal jesteśmy na stronie
        # logowania, logowanie się nie powiodło.
        if "Podaj hasło" in response.text:
            raise UpstreamError("Logowanie do plan.ckziu.jaworzno.pl nie powiodło się")

    return session


def get_zastepstwa_session() -> requests.Session:
    """
    Zaloguj się do https://www.ckziu.jaworzno.pl/zastepstwa/ i zwróć sesję.

    Strona WordPressowa - formularz logowania to POST z polami
    'password' i 'check' na ten sam URL, sesja trzymana w cookies.
    """
    if not SCHOOL_PASSWORD:
        raise UpstreamError("Brak SCHOOL_PASSWORD w zmiennych środowiskowych")

    session = requests.Session()
    response = session.post(
        ZASTEPSTWA_URL,
        data={"password": SCHOOL_PASSWORD, "check": "Sprawdź"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    if "Podaj hasło" in response.text:
        raise UpstreamError("Logowanie do /zastepstwa/ nie powiodło się")

    return session


def fetch_zastepstwa_html(session: requests.Session) -> str:
    """Pobierz HTML strony zastępstw, z jednorazowym retry po ponownym logowaniu."""
    response = session.get(ZASTEPSTWA_URL, timeout=REQUEST_TIMEOUT)
    if "Podaj hasło" in response.text:
        # Sesja wygasła w trakcie - zaloguj się jeszcze raz.
        session = get_zastepstwa_session()
        response = session.get(ZASTEPSTWA_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if "Podaj hasło" in response.text:
        raise UpstreamError("Upstream /zastepstwa/ nadal zwraca formularz logowania")
    return response.text


# ---------------------------------------------------------------------------
# Normalizacja nazwisk
# ---------------------------------------------------------------------------

# Tytuły/prefiksy, które mogą występować przed nazwiskiem w Firestore.
# Kolejność ma znaczenie - dłuższe/bardziej specyficzne najpierw.
_TITLE_PREFIXES = [
    "mgr inż.",
    "mgr. inż.",
    "mgr inz.",
    "dr inż.",
    "dr hab.",
    "mgr.",
    "mgr",
    "dr.",
    "dr",
    "inż.",
    "inż",
]

_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(p) for p in _TITLE_PREFIXES) + r")\s+",
    flags=re.IGNORECASE,
)


def normalize_teacher_name(raw_name: str) -> str:
    """
    Sprowadza nazwisko do formy kanonicznej "Nazwisko Imię" (lub
    "Nazwisko-Złożone Imię"), usuwając tytuły naukowe i nadmiarowe spacje.

    Przykłady:
        "mgr. Kowalczyk Mirosław"  -> "Kowalczyk Mirosław"
        "mgr inż. Stawski Jakub"   -> "Stawski Jakub"
        "  Głowacka   Patrycja "   -> "Głowacka Patrycja"
    """
    if not raw_name:
        return ""

    name = raw_name.strip()

    # Usuń prefiks tytułu, jeśli występuje (może wystąpić wielokrotnie,
    # np. "dr mgr inż." - mało prawdopodobne, ale pętla jest bezpieczna).
    while True:
        new_name = _TITLE_PREFIX_RE.sub("", name)
        if new_name == name:
            break
        name = new_name

    # Zredukuj wielokrotne spacje.
    name = " ".join(name.split())

    return name


def names_match(name_a: str, name_b: str) -> bool:
    """Porównanie odporne na tytuły, wielkość liter i białe znaki."""
    return normalize_teacher_name(name_a).casefold() == normalize_teacher_name(name_b).casefold()


# ---------------------------------------------------------------------------
# Helpery BeautifulSoup (współdzielone z parserem planu)
# ---------------------------------------------------------------------------

def direct_text(element) -> str:
    return " ".join(
        text.strip()
        for text in element.find_all(string=True, recursive=False)
        if text.strip()
    )


def normalized_text(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split()) if element else ""
