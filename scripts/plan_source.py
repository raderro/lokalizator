"""
Pobieranie i parsowanie planu lekcji z plan.ckziu.jaworzno.pl.

To jest przeniesiona logika z oryginalnego plan_api.py (parser HTML/aSc
Plan Lekcji), ale bez serwera HTTP - funkcje są wywoływane bezpośrednio
przez aktualizuj_statusy.py i build_teacher_mapping.py.
"""

from __future__ import annotations

from typing import Any, Literal

import requests
from bs4 import BeautifulSoup

from common import (
    PLAN_BASE_URL,
    REQUEST_TIMEOUT,
    UpstreamError,
    direct_text,
    get_plan_session,
    normalized_text,
)


def _parse_time_label(label: str) -> dict[str, str]:
    slot_label, _, time_range = label.partition(" ")
    parts = time_range.split("-")
    if len(parts) != 2:
        return {"label": label, "slot_label": slot_label}
    return {
        "label": label,
        "slot_label": slot_label,
        "time": time_range.strip(),
        "start": parts[0].strip(),
        "end": parts[1].strip(),
    }


def _parse_lesson_block(block) -> dict[str, Any]:
    lesson_classes = block.get("class", [])
    lesson_type: Literal["lesson", "supervision"] = "lesson"
    if block.select_one(".supervision") or "supervision" in lesson_classes:
        lesson_type = "supervision"

    if lesson_type == "supervision":
        return {
            "type": lesson_type,
            "text": normalized_text(block),
            "color": block.get("style", ""),
        }

    full_info = block.select_one(".full-info")
    teacher_full = full_info.select_one(".teacher") if full_info else None
    classroom_full = full_info.select_one(".classroom") if full_info else None
    subject_full = full_info.select_one(".subject-full") if full_info else None
    group_full = full_info.select_one(".group") if full_info else None

    return {
        "type": lesson_type,
        "lesson_class": next((name for name in lesson_classes if name.startswith("lesson-")), None),
        "teacher_short": normalized_text(block.select_one(".teacher")),
        "classroom_short": normalized_text(block.select_one(".classroom")),
        "subject_short": normalized_text(block.select_one(".subject")),
        "group": normalized_text(block.select_one(".group")),
        "teacher_full": normalized_text(teacher_full),
        "classroom_full": normalized_text(classroom_full),
        "subject_full": normalized_text(subject_full),
        "group_full": normalized_text(group_full),
        "color": block.get("style", ""),
    }


def parse_timetable(html: str, kind: str, entity_id: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    timetable = soup.select_one(".timetable")
    if timetable is None:
        raise UpstreamError("Nie udało się odczytać planu z upstreamu (brak .timetable)")

    name_node = timetable.select_one(".class-name")
    date_node = timetable.select_one(".date")
    entity_name = direct_text(name_node) if name_node else ""
    updated_at = normalized_text(date_node) if date_node else ""
    slot_labels = [
        _parse_time_label(normalized_text(node))
        for node in soup.select(".hours .half-card")
        if normalized_text(node)
    ]

    days = []
    for day_group in timetable.select(".day-with-cards"):
        header = day_group.select_one(".day")
        if header is None:
            continue

        day_name = normalized_text(header.select_one(".big-size"))
        day_short = normalized_text(header.select_one(".md-size"))
        slots = []

        for slot_index, card in enumerate(day_group.find_all("div", class_="card", recursive=False), start=1):
            slot_time = slot_labels[slot_index - 1] if slot_index - 1 < len(slot_labels) else {}
            direct_blocks = [block for block in card.find_all("div", recursive=False) if block.name == "div"]
            parsed_lessons = []

            for block in direct_blocks:
                if not normalized_text(block):
                    continue
                lesson = _parse_lesson_block(block)
                lesson["slot"] = slot_index
                lesson["time"] = slot_time
                parsed_lessons.append(lesson)

            slots.append(
                {
                    "slot": slot_index,
                    "time": slot_time,
                    "lessons": parsed_lessons,
                    "lesson_count": len(parsed_lessons),
                }
            )

        days.append(
            {
                "name": day_name,
                "short_name": day_short,
                "slots": slots,
                "lessons_count": sum(slot["lesson_count"] for slot in slots),
                "lessons": [lesson for slot in slots for lesson in slot["lessons"]],
            }
        )

    return {
        "kind": kind,
        "id": entity_id,
        "name": entity_name,
        "updated_at": updated_at,
        "source_url": f"{PLAN_BASE_URL}/?{kind}id={entity_id}",
        "days": days,
    }


def fetch_plan_html(session: requests.Session, params: dict[str, str]) -> str:
    response = session.get(PLAN_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if "Podaj hasło" in response.text:
        raise UpstreamError("Upstream planu zwrócił stronę logowania zamiast planu")
    return response.text


def get_schedule(session: requests.Session, kind: Literal["class", "teacher"], entity_id: str) -> dict[str, Any]:
    html = fetch_plan_html(session, {f"{kind}id": entity_id})
    return parse_timetable(html, kind, entity_id)


def fetch_homepage_html(session: requests.Session) -> str:
    """Strona główna planu (bez parametrów) - używana do zbudowania mapowania nauczycieli."""
    response = session.get(PLAN_BASE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if "Podaj hasło" in response.text:
        raise UpstreamError("Upstream planu zwrócił stronę logowania zamiast strony głównej")
    return response.text


__all__ = [
    "get_plan_session",
    "get_schedule",
    "fetch_homepage_html",
    "parse_timetable",
]
