"""Shared acceptance-criteria extraction for issue-tracker intake."""

from __future__ import annotations

import logging
import re


log = logging.getLogger("no_human.intake.criteria")

_CHECKLIST_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$")
_HEADING = re.compile(
    r"^\s*(?P<hashes>#{1,6})\s*acceptance\s+criteria\s*#*\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^\s*(?P<hashes>#{1,6})\s+")
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def extract_acceptance_criteria(text: str, issue_name: str) -> list[str]:
    """Extract checkboxes, or plain bullets from an acceptance-criteria section.

    Checkboxes remain the authoritative format wherever they occur, preserving
    the behavior that existing imported tasks rely on.  The section fallback
    serves repositories whose issue template uses ordinary Markdown bullets.
    """
    lines = (text or "").splitlines()
    checklist = [match.group(1).strip() for line in lines
                 if (match := _CHECKLIST_ITEM.match(line))]
    if checklist:
        return checklist

    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if not heading:
            continue
        level = len(heading.group("hashes"))
        criteria: list[str] = []
        for section_line in lines[index + 1:]:
            next_heading = _ANY_HEADING.match(section_line)
            if next_heading and len(next_heading.group("hashes")) <= level:
                break
            if bullet := _BULLET.match(section_line):
                criteria.append(bullet.group(1).strip())
        if criteria:
            return criteria
        log.warning(
            "%s has an Acceptance criteria heading but no extractable criteria; "
            "pass --criteria explicitly.", issue_name)
        return []
    return []
