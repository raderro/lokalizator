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

WWW_BASE_URL = "https://www.ckziu.jaworzno.pl"
ZASTEPSTWA_URL = f"{WWW_BASE_URL}/zastepstwa/"

# Domyślny User-Agent przeglądarki - niektóre serwery (w tym WordPress
# z pewnymi wtyczkami bezpieczeństwa) blokują domyślny User-Agent
# biblioteki `requests`.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

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
    session.headers.update(DEFAULT_HEADERS)

    response = session.post(
        PLAN_LOGIN_URL,
        data={"password": SCHOOL_PASSWORD},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    if "Podaj hasło" in response.text:
        raise UpstreamError("Logowanie do plan.ckziu.jaworzno.pl nie powiodło się")

    return session


def get_zastepstwa_session() -> requests.Session:
    """
    Zaloguj się do https://www.ckziu.jaworzno.pl/zastepstwa/ i zwróć sesję.

    Strona "/zastepstwa/" ma WŁASNY formularz logowania (nie standardowy
    mechanizm WordPress "post password"). Formularz wygląda tak:

        <form method="POST" action="">
          <input type="password" name="pass">
          <input type="submit" name="submit" value="Sprawdź">
        </form>

    `action=""` oznacza POST na ten sam adres (https://.../zastepstwa/).
    Po prawidłowym haśle serwer ustawia sesję (cookie PHP) i przy
    kolejnych żądaniach GET na "/zastepstwa/" zwraca już treść zastępstw
    zamiast formularza "Podaj hasło:".
    """
    if not SCHOOL_PASSWORD:
        raise UpstreamError("Brak SCHOOL_PASSWORD w zmiennych środowiskowych")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    response = session.post(
        ZASTEPSTWA_URL,
        data={
            "pass": SCHOOL_PASSWORD,
            "submit": "Sprawdź",
        },
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    if "Podaj hasło" in response.text:
        raise UpstreamError(
            "Logowanie do /zastepstwa/ nie powiodło się - "
            "po POST z polami pass/submit serwer wciąż zwraca formularz logowania"
        )

    return session


def fetch_zastepstwa_html(session: requests.Session) -> str:
    """Pobierz HTML strony zastępstw, z jednorazowym retry po ponownym logowaniu."""
    response = session.get(ZASTEPSTWA_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

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