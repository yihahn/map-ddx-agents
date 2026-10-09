import argparse
import re
import sqlite3
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Input: the StatPearls book archive from NLM's LitArch FTP service
# (https://ftp.ncbi.nlm.nih.gov/pub/litarch/3d/12/statpearls_NBK430685.tar.gz, ~2 GB, one BITS XML
# file per chapter plus images). Output: data_prep/statpearls/statpearls.sqlite, holding every
# chapter's title and the text of each of its first- and second-level sections, keyed by a
# normalised title so module 3 can look a chapter up by the title E-utilities reports for it.
# Algorithm: stream the archive once without unpacking it, parse only the .nxml members, and for
# each section render its paragraphs, nested subsections and tables as plain text. Which sections
# matter is decided by the reader (STATPEARLS_SECTIONS in modules/module3_deterministic/
# criteria.py), not here, so the selection rule lives in one place. This archive replaces scraping
# the Bookshelf web pages: Bookshelf forbids crawlers and automated processes from retrieving its
# site content, names LitArch as the only service that may be used for automated download, and
# began enforcing that with a reCAPTCHA page (served as HTTP 200) between 2026-09-07 and -09-22.

ROOT = Path(__file__).resolve().parent / "statpearls"
ARCHIVE = ROOT / "statpearls_NBK430685.tar.gz"
DATABASE = ROOT / "statpearls.sqlite"


def normalise_title(title: str) -> str:
    """The key a chapter is filed and looked up under: case and spacing do not distinguish titles."""
    return " ".join(title.split()).lower()


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _render(element: ET.Element, out: list[str], top: ET.Element) -> None:
    """Append a section's readable content in document order.

    Paragraphs and table rows are taken whole and their descendants are not visited again, so a
    paragraph inside a table cell or a list item is rendered once. Subsection titles are kept as
    lines of their own, because a subsection is often where a threshold sits ("Laboratory Studies").
    """
    for child in element:
        tag = child.tag
        if tag == "title":
            if element is not top:
                out.append(_text(child))
        elif tag == "p":
            text = _text(child)
            if text:
                out.append(text)
        elif tag == "tr":
            cells = [_text(cell) for cell in child if cell.tag in ("td", "th")]
            if any(cells):
                out.append(" | ".join(cells))
        elif tag in ("ref-list", "fn-group", "graphic", "media", "xref", "label"):
            continue
        else:
            _render(child, out, top)


def _sections(body: ET.Element):
    """Yield (depth, name, text) for every first- and second-level section of a chapter body.

    Two levels because the web pages this replaces were read at h2 and h3 alike: a drug chapter
    files its "Toxicity" as a subsection, and stopping at the first level would lose it.
    """
    for top in body.findall("sec"):
        for depth, sec in [(1, top)] + [(2, sub) for sub in top.findall("sec")]:
            title = sec.find("title")
            if title is None:
                continue
            lines: list[str] = []
            _render(sec, lines, sec)
            text = "\n".join(lines)
            if text:
                yield depth, _text(title), text


def build(archive: Path = ARCHIVE, database: Path = DATABASE) -> tuple[int, int]:
    database.unlink(missing_ok=True)
    db = sqlite3.connect(database)
    db.executescript(
        """
        CREATE TABLE chapters (article_id TEXT PRIMARY KEY, title TEXT, title_key TEXT);
        CREATE INDEX chapters_by_title ON chapters (title_key);
        CREATE TABLE sections (article_id TEXT, ordinal INTEGER, depth INTEGER, name TEXT, text TEXT);
        CREATE INDEX sections_by_article ON sections (article_id);
        """
    )
    chapters = sections = 0
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".nxml"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            root = ET.parse(handle).getroot()
            part = root.find("book-part")
            title = part.find("book-part-meta/title-group/title") if part is not None else None
            body = part.find("body") if part is not None else None
            if title is None or body is None:
                continue
            article_id = root.get("id") or Path(member.name).stem
            name = _text(title)
            db.execute("INSERT OR REPLACE INTO chapters VALUES (?, ?, ?)",
                       (article_id, name, normalise_title(name)))
            for ordinal, (depth, heading, text) in enumerate(_sections(body)):
                db.execute("INSERT INTO sections VALUES (?, ?, ?, ?, ?)",
                           (article_id, ordinal, depth, heading, text))
                sections += 1
            chapters += 1
    db.commit()
    db.close()
    return chapters, sections


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the StatPearls LitArch archive by chapter title.")
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    chapters, sections = build(args.archive, args.database)
    print(f"{chapters} chapters, {sections} sections -> {args.database}")


if __name__ == "__main__":
    main()
