"""
Fit, eligibility and value estimation.

The arena states how points are earned (GET /requisitions, `points_note`):
    "points scale with skill match, assessment margin over the bar, recruiter
     notes, notice period and salary headroom; below the bar earns nothing"
So a candidate's value is modelled as those five terms, each normalised to
[0, 1], and a candidate who fails any part of the bar is worth zero.
"""
import parsing as P

# Relative weight of each points term. The arena publishes the terms but not
# their weights, so they start equal; assessment margin is nudged up because it
# is the only term with a verified value behind it.
WEIGHTS = {"skills": 0.22, "assessment": 0.26, "notes": 0.16, "notice": 0.16, "salary": 0.20}

_NOTE_POSITIVE = [
    "strong ownership", "drove", "shipped", "mentored", "led the", "led ", "owned",
    "excellent", "outstanding", "consistently delivered", "root-cause", "root cause",
    "on-call", "impressive", "strong communicator", "scaled", "architected", "exceeded",
    "strong system design", "strong ", "promoted", "top performer", "high ownership",
]
_NOTE_NEGATIVE = [
    "struggled", "lukewarm", "unclear", "needed frequent guidance", "missed",
    "could not", "couldn't", "weak", "concern", "declined", "no-show", "no show",
    "vague", "shallow", "red flag", "inconsistent", "poor", "late", "disengaged",
]
# Round 1 found that stacking two templated achievement clauses into one note
# marks an over-claimed (often fabricated) profile. Kept here as a risk signal.
_PRAISE_CLAUSES = [
    "consistently delivered", "shipped a feature", "strong ownership",
    "mentored two", "led the migration", "hiring manager flagged excellent", "strong system design",
    "owned production incidents", "carried the pager", "handled 24x7",
]

_CITY_ALIASES = {"bangalore": "bengaluru", "bombay": "mumbai", "gurgaon": "gurugram",
                 "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi"}


def note_score(note):
    """Recruiter note -> [0, 1]; 0.5 is neutral or empty."""
    if not note:
        return 0.5
    t = str(note).lower()
    pos = sum(p in t for p in _NOTE_POSITIVE)
    neg = sum(n in t for n in _NOTE_NEGATIVE)
    return max(0.0, min(1.0, 0.5 + 0.15 * pos - 0.25 * neg))


def praise_stack(note):
    """Two or more templated achievement clauses in one note."""
    t = str(note or "").lower()
    return sum(p in t for p in _PRAISE_CLAUSES) >= 2


def skill_match(candidate_skills, req):
    wanted = {P.normalise_skill(s) for s in req["skills"]}
    return len(candidate_skills & wanted) / max(1, len(wanted))


def person_key(profile_or_summary):
    """
    Identity for de-duplication. The pool carries no phone or email, so a person
    is name + city + whole years of experience: re-entered duplicates differ
    cosmetically, while two different people rarely share all three.
    """
    rec = profile_or_summary
    city = str(rec.get("city") or "").strip().lower()
    city = _CITY_ALIASES.get(city, city)
    yrs = rec.get("experience")
    if not isinstance(yrs, (int, float)):
        yrs = P.parse_experience_years(yrs)
    return f"{P.norm_name(rec.get('name'))}|{city}|{'' if yrs is None else round(yrs)}"


def read_profile(raw):
    """Parse a raw /candidate record once, into the numbers the agent reasons with."""
    return {
        "candidate_id": raw.get("candidate_id"),
        "name": raw.get("name"),
        "role": raw.get("role"),
        "city": raw.get("city"),
        "experience": P.parse_experience_years(raw.get("experience")),
        "skills": P.parse_skills(raw.get("skills")),
        "current_ctc": P.parse_ctc_lpa(raw.get("current_ctc")),
        "expected_ctc": P.parse_ctc_lpa(raw.get("expected_ctc")),
        "notice_days": P.parse_notice_days(raw.get("notice_period")),
        "assessment": P.parse_assessment(raw.get("assessment")),
        "note": raw.get("note") or "",
        "claimed": bool(raw.get("claimed")),
    }


def fabrication_risk(p):
    """
    Cheap, pre-assessment reasons to distrust a profile. Each returns a label;
    an empty list means nothing looks off. These only decide whether a
    candidate is worth 25 credits of verification - /assess is the final word.
    """
    risks = []
    if praise_stack(p["note"]):
        risks.append("praise_stack")
    cur, exp = p["current_ctc"], p["expected_ctc"]
    if cur and exp and cur > 0 and exp / cur > 3.0:
        risks.append("ctc_jump")
    a = p["assessment"]
    if a is not None and (a > 100 or a < 0):
        risks.append("assessment_out_of_range")
    yrs = p["experience"]
    if yrs is not None and (yrs < 0 or yrs > 45):
        risks.append("experience_impossible")
    return risks


def bar_failures(p, req, assessment=None):
    """
    Every reason the candidate misses the requisition's bar. Empty = eligible.
    Missing values count as failures: an unknown notice period or CTC cannot be
    shown to meet the bar, and a wrong guess costs a slot.
    """
    fails = []
    if str(p["role"]).strip().lower() != str(req["role"]).strip().lower():
        fails.append("role")
    score = assessment if assessment is not None else p["assessment"]
    if score is None or score < req["min_assessment"]:
        fails.append("assessment")
    if p["notice_days"] is None or p["notice_days"] > req["max_notice_days"]:
        fails.append("notice")
    if p["expected_ctc"] is None or p["expected_ctc"] > req["max_expected_ctc_lpa"]:
        fails.append("ctc")
    return fails


def estimated_points(p, req, assessment=None):
    """Relative value of signing this candidate to this requisition, in [0, 1]. Zero if below the bar."""
    if bar_failures(p, req, assessment):
        return 0.0
    score = assessment if assessment is not None else p["assessment"]
    terms = {
        "skills": skill_match(p["skills"], req),
        "assessment": min(1.0, (score - req["min_assessment"]) / max(1.0, 100.0 - req["min_assessment"])),
        "notes": note_score(p["note"]),
        "notice": 1.0 - p["notice_days"] / max(1.0, req["max_notice_days"]),
        "salary": (req["max_expected_ctc_lpa"] - p["expected_ctc"]) / max(1.0, req["max_expected_ctc_lpa"]),
    }
    return sum(WEIGHTS[k] * max(0.0, min(1.0, v)) for k, v in terms.items())


def assessment_passes(assess_record, req):
    """Read a GET /assess result. Returns (ok, verified_score, reason)."""
    if not assess_record:
        return False, None, "no_assessment"
    if assess_record.get("identity_verified") is False:
        return False, None, "identity_not_verified"
    ref = str(assess_record.get("reference_check", "")).strip().lower()
    if ref and ref != "clean":
        return False, None, f"reference_{ref}"
    score = P.parse_assessment(assess_record.get("verified_assessment"))
    if score is None:
        return False, None, "no_verified_score"
    if score < req["min_assessment"]:
        return False, score, "verified_below_bar"
    return True, score, "ok"


# ---------------------------------------------------------------- the arena's points, as measured
# Calibrated on the live ledger: across our first 116 signings, sum(verified - bar) = 1,884
# against 1,974 points actually scored, i.e. points track the verified assessment margin
# one-for-one plus about 0.78 each. Skill match is kept as a multiplier because every one
# of those signings matched 6 of 6, so the ledger cannot yet say what a partial match is
# worth; being conservative about it costs little. The five-term estimate breaks ties.
MARGIN_BONUS = 0.78


def predicted_points(p, req, assessment):
    """Expected arena points for signing this candidate to this requisition."""
    if assessment is None or bar_failures(p, req, assessment):
        return 0.0
    margin = assessment - req["min_assessment"]
    return margin * skill_match(p["skills"], req) + MARGIN_BONUS + 0.5 * estimated_points(p, req, assessment)
