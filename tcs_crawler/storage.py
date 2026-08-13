"""落盘：JSONL / CSV / SQLite。"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path

from .models import Paper

log = logging.getLogger(__name__)

CSV_FIELDS = [
    "venue", "year", "title", "authors", "num_authors", "pages",
    "doi", "ee", "dblp_key", "dblp_url", "type", "access", "venue_full", "toc_key",
]


def write_jsonl(papers: list[Paper], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for p in papers:
            f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
    log.info("写入 %s (%d 条)", path, len(papers))


def write_csv(papers: list[Paper], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for p in papers:
            writer.writerow(p.to_row())
    log.info("写入 %s (%d 条)", path, len(papers))


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    dblp_key   TEXT PRIMARY KEY,
    venue      TEXT NOT NULL,
    venue_full TEXT,
    year       INTEGER,
    title      TEXT NOT NULL,
    authors    TEXT,
    num_authors INTEGER,
    pages      TEXT,
    doi        TEXT,
    ee         TEXT,
    dblp_url   TEXT,
    type       TEXT,
    access     TEXT,
    toc_key    TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_venue_year ON papers(venue, year);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);

CREATE TABLE IF NOT EXISTS authors (
    pid   TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name);

CREATE TABLE IF NOT EXISTS paper_authors (
    dblp_key TEXT NOT NULL,
    pid      TEXT,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (dblp_key, position)
);
CREATE INDEX IF NOT EXISTS idx_pa_pid ON paper_authors(pid);
CREATE INDEX IF NOT EXISTS idx_pa_name ON paper_authors(name);
"""


def write_sqlite(papers: list[Paper], path: Path, replace_venues: list[str] | None = None) -> None:
    """写入 SQLite。replace_venues 里的会议会先清空，便于增量重跑。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        if replace_venues:
            marks = ",".join("?" * len(replace_venues))
            conn.execute(
                f"DELETE FROM paper_authors WHERE dblp_key IN "
                f"(SELECT dblp_key FROM papers WHERE venue IN ({marks}))",
                replace_venues,
            )
            conn.execute(f"DELETE FROM papers WHERE venue IN ({marks})", replace_venues)

        for p in papers:
            if not p.dblp_key:
                continue
            row = p.to_row()
            conn.execute(
                """INSERT OR REPLACE INTO papers
                   (dblp_key, venue, venue_full, year, title, authors, num_authors,
                    pages, doi, ee, dblp_url, type, access, toc_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.dblp_key, row["venue"], row["venue_full"], row["year"], row["title"],
                    row["authors"], row["num_authors"], row["pages"], row["doi"], row["ee"],
                    row["dblp_url"], row["type"], row["access"], row["toc_key"],
                ),
            )
            conn.execute("DELETE FROM paper_authors WHERE dblp_key = ?", (p.dblp_key,))
            for i, a in enumerate(p.authors):
                if a.pid:
                    conn.execute(
                        "INSERT OR REPLACE INTO authors (pid, name) VALUES (?,?)",
                        (a.pid, a.clean_name),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO paper_authors (dblp_key, pid, name, position) "
                    "VALUES (?,?,?,?)",
                    (p.dblp_key, a.pid, a.clean_name, i),
                )
        conn.commit()
    finally:
        conn.close()
    log.info("写入 %s (%d 条)", path, len(papers))
