"""
Parsers for the arena's messy profile fields.

The pool is as messy as Round 1's Archive, so these are the Round 1 parsers
(copied, not imported - the prelims code stays untouched), adapted to the
fields the arena actually returns. Every parser returns None rather than
raising when a value cannot be read: a missing value must never crash the
agent, and must never be mistaken for a zero.
"""
import re

_NUM = re.compile(r"(\d+(?:\.\d+)?)")


def first_number(text):
    """First number in a messy string, or None. '71/100' -> 71, '45 days' -> 45."""
    if text is None:
        return None
    m = _NUM.search(str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def parse_experience_years(text):
    """'2.6 yrs', '13.5', '8.7 years', '56 months', 'Fresher', '<1 year' -> years."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    if "fresher" in t:
        return 0.0
    v = first_number(t)
    if v is None:
        return None
    if "month" in t or re.search(r"\bmos?\b", t):
        return v / 12.0
    if t.startswith("<"):
        return v / 2.0
    return v


def parse_assessment(text):
    """'56', '45/100', '91.0 %' -> score out of 100. 'not taken' / 'N/A' -> None."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t or t in {"n/a", "na", "not taken", "absent", "-", "none", "null"}:
        return None
    v = first_number(t)
    if v is None:
        return None
    m = re.search(r"/\s*(\d+(?:\.\d+)?)", t)          # explicit denominator: 7/10 -> 70
    if m and float(m.group(1)) > 0:
        return v * 100.0 / float(m.group(1))
    if "%" not in t and v <= 1.0:                       # fraction of one: '0.85' -> 85
        return v * 100.0
    return v


def parse_ctc_lpa(text):
    """
    Compensation in lakhs per annum.
    '2260000' -> 22.6, '22.6 LPA' -> 22.6, 'INR 29.1 lakh' -> 29.1, '1.2 Cr' -> 120, '$30,000' -> 25.5
    """
    if text is None:
        return None
    t = str(text).strip().lower().replace(",", "")
    if not t:
        return None
    v = first_number(t)
    if v is None:
        return None
    if "$" in t or "usd" in t:
        return v * 85.0 / 1e5                  # assumed USD->INR rate, declared in the notes
    if "cr" in t:
        return v * 100.0
    if "lpa" in t or "lakh" in t or "lac" in t or re.search(r"\d\s*l\b", t):
        return v
    if v >= 1e5:                               # bare rupees
        return v / 1e5
    return v                                   # already lakhs


def parse_notice_days(text):
    """'30 days', '1 month', 'Immediate', 'Serving notice - 15 days' -> days. A number beats a word."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    v = first_number(t)
    if v is not None:
        if "month" in t:
            return v * 30.0
        if "week" in t:
            return v * 7.0
        return v
    if "immediate" in t or "available now" in t or "joiner" in t:
        return 0.0
    return None


# Skill spellings seen in the pool, mapped onto the requisitions' vocabulary.
_SKILL_ALIASES = {
    "postgres": "postgresql", "postgre": "postgresql", "psql": "postgresql",
    "rest": "rest apis", "rest api": "rest apis", "restful": "rest apis", "restful apis": "rest apis",
    "js": "javascript", "ts": "typescript", "reactjs": "react", "react.js": "react",
    "node": "node.js", "nodejs": "node.js", "node js": "node.js",
    "sklearn": "scikit-learn", "scikit learn": "scikit-learn", "scikit": "scikit-learn",
    "k8s": "kubernetes", "kube": "kubernetes",
    "ml ops": "mlops", "ml-ops": "mlops",
    "ci cd": "ci/cd", "cicd": "ci/cd", "ci-cd": "ci/cd",
    "amazon web services": "aws",
    "ab testing": "a/b testing", "a-b testing": "a/b testing", "a/b test": "a/b testing",
    "stats": "statistics", "statistical analysis": "statistics",
    "py": "python", "python3": "python",
    "micro services": "microservices", "micro-services": "microservices",
    "html5": "html", "css3": "css",
    "apache spark": "spark", "pyspark": "spark", "apache airflow": "airflow", "apache kafka": "kafka",
    "torch": "pytorch", "ms excel": "excel", "microsoft excel": "excel",
}


def normalise_skill(raw):
    s = re.sub(r"\s+", " ", str(raw).strip().lower())
    return _SKILL_ALIASES.get(s, s)


def parse_skills(text):
    """'Redis | Mongodb | Git', 'Django, Java', 'Excel; Postgres' -> set of normalised skills."""
    if not text:
        return set()
    if isinstance(text, list):
        parts = text
    else:
        parts = re.split(r"[,;|/]\s*|\s+\|\s+", str(text))
        # the '/' split would break 'ci/cd' and 'a/b testing': rejoin those
        joined = str(text).lower()
        extra = [s for s in ("ci/cd", "a/b testing") if s in joined]
        parts = parts + extra
    return {normalise_skill(p) for p in parts if str(p).strip()}


def norm_name(name):
    return re.sub(r"[^a-z]", "", str(name or "").lower())
