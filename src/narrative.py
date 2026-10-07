"""
Narrative evidence retrieval for the qualitative agent (the "RAG" step), with no extra dependencies.

  filing HTML -> plain text -> credit-relevant sections (Risk Factors, Legal Proceedings, Liquidity) ->
  paragraph chunks with stable ids -> keyword retrieval per credit topic -> a small, bounded evidence set.

The LLM is only ever shown this evidence, and every risk it reports must quote a chunk verbatim
(see qual_agent.ground), so a finding can always be traced to the filing text it came from.
"""
import math
import re
from typing import Any, Dict, List, Tuple

from lxml import html as lxml_html

MAX_CHUNK_CHARS = 1800
MAX_SECTION_CHARS = 600_000
MAX_EVIDENCE_CHARS = 16_000          # keeps the prompt inside Groq's per-request token limits
PER_TOPIC = 2
MIN_SCORE = 2.0

# section label -> (start heading regex, end heading regex). Annual reports repeat headings in the table of
# contents, so the LONGEST start->end span wins. Foreign (20-F) filers use different headings; if none match,
# the whole text is searched instead.
SECTIONS: Dict[str, Tuple[str, str]] = {
    "Risk Factors": (r"item\s*1a\.?\s*[-–—:]?\s*risk\s+factors", r"item\s*1b\.?|item\s*1c\.?|item\s*2\.?\s*[-–—:]?\s*properties"),
    "Legal Proceedings": (r"item\s*3\.?\s*[-–—:]?\s*legal\s+proceedings", r"item\s*4\.?\s*[-–—:]?\s*mine"),
    "Liquidity and Capital Resources": (r"liquidity\s+and\s+capital\s+resources",
                                        r"critical\s+accounting|off[-\s]balance\s+sheet|item\s*7a\.?|recent\s+accounting"),
}

# credit topic -> keywords (matched case-insensitively as substrings)
TOPICS: Dict[str, List[str]] = {
    "going concern": ["substantial doubt", "going concern"],
    "covenant / default": ["event of default", "covenant", "waiver", "acceleration", "cross-default", "in default",
                           "not in compliance"],
    "litigation / regulatory": ["legal proceedings", "class action", "lawsuit", "litigation", "settlement",
                                "subpoena", "investigation", "penalt", "injunction"],
    "liquidity / refinancing": ["refinanc", "maturit", "liquidity", "revolving", "credit facility",
                                "commercial paper", "credit rating", "downgrade"],
    "concentration": ["significant customer", "largest customer", "customer concentration", "sole source",
                      "single supplier", "% of total revenue", "% of revenue", "% of net sales"],
    "controls / restatement": ["material weakness", "restate", "restatement", "internal control over financial"],
}

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}


def html_to_text(content: bytes) -> str:
    """Visible text of an (inline-XBRL) HTML filing, one paragraph per line."""
    root = lxml_html.fromstring(content)
    for el in list(root.iter()):
        tag = el.tag if isinstance(el.tag, str) else ""
        style = (el.get("style") or "").replace(" ", "").lower() if tag else ""
        if tag in ("script", "style", "ix:header") or "display:none" in style:
            el.drop_tree()
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in _BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
            if el.tag.lower() != "br":
                el.text = (el.text or "")
    text = root.text_content().replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def find_sections(text: str) -> Dict[str, str]:
    """Longest heading-to-heading span for each known section. Missing sections are simply absent."""
    found: Dict[str, str] = {}
    for label, (start_re, end_re) in SECTIONS.items():
        best = ""
        starts = [m.start() for m in re.finditer(start_re, text, re.I)]
        for s in starts:
            m_end = re.search(end_re, text[s + 200:], re.I)
            e = s + 200 + m_end.start() if m_end else min(len(text), s + MAX_SECTION_CHARS)
            span = text[s:min(e, s + MAX_SECTION_CHARS)]
            if len(span) > len(best):
                best = span
        if len(best) > 1500:                      # a table-of-contents hit is only a few dozen characters
            found[label] = best
    return found


def chunk_section(label: str, text: str, start_index: int = 1) -> List[Dict[str, str]]:
    chunks: List[Dict[str, str]] = []
    buf = ""

    def flush():
        nonlocal buf
        if len(buf.strip()) >= 80:
            chunks.append({"id": f"C{start_index + len(chunks):04d}", "section": label, "text": buf.strip()})
        buf = ""

    for line in text.split("\n"):
        while len(line) > MAX_CHUNK_CHARS:            # very long single paragraph: cut at a sentence/space
            cut = max(line.rfind(". ", 0, MAX_CHUNK_CHARS), line.rfind(" ", 0, MAX_CHUNK_CHARS))
            cut = cut if cut > MAX_CHUNK_CHARS // 2 else MAX_CHUNK_CHARS
            if buf:
                flush()
            buf = line[:cut + 1]
            flush()
            line = line[cut + 1:].lstrip()
        if len(buf) + len(line) + 1 > MAX_CHUNK_CHARS:
            flush()
        buf = f"{buf}\n{line}" if buf else line
    flush()
    return chunks


def _score(text: str, keywords: List[str]) -> float:
    low = text.lower()
    s = 0.0
    for kw in keywords:
        n = low.count(kw)
        if n:
            s += 1.0 + math.log(n)                    # distinct keywords count most; repeats are damped
    return s


def retrieve(chunks: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Best chunks per credit topic, de-duplicated and capped at MAX_EVIDENCE_CHARS."""
    picked: Dict[str, Dict[str, str]] = {}
    for topic, kws in TOPICS.items():
        ranked = sorted(((_score(c["text"], kws), c) for c in chunks), key=lambda t: -t[0])
        for score, c in ranked[:PER_TOPIC]:
            if score >= MIN_SCORE and c["id"] not in picked:
                picked[c["id"]] = dict(c, topic=topic)
    out, total = [], 0
    for c in picked.values():
        if total + len(c["text"]) > MAX_EVIDENCE_CHARS:
            continue
        out.append(c)
        total += len(c["text"])
    return out


def build_evidence(content: bytes) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """(evidence chunks, info about what was found) for a filing document."""
    text = html_to_text(content)
    sections = find_sections(text)
    chunks: List[Dict[str, str]] = []
    if sections:
        for label, body in sections.items():
            chunks += chunk_section(label, body, start_index=len(chunks) + 1)
    else:
        chunks = chunk_section("Filing text (no standard Item headings found)", text)
    info = {"sections": {k: len(v) for k, v in sections.items()}, "chunks_searched": len(chunks)}
    return retrieve(chunks), info


def attach(dossier: Dict[str, Any], content: bytes) -> Dict[str, Any]:
    """Add retrieved narrative evidence to a loader dossier (in place)."""
    evidence, info = build_evidence(content)
    dossier["narrative"] = dict(info, evidence_chunks=len(evidence))
    if evidence:
        dossier["qualitative_evidence"] = evidence
        dossier["qualitative_available"] = True
        dossier["qualitative_notes"] = ""             # the analyst may add notes here; they are treated as extra evidence
    else:
        dossier.setdefault("warnings", []).append(
            "No credit-relevant narrative passages were found in the filing text; the qualitative step was skipped.")
    return dossier
