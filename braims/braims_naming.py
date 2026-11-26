"""BRAIMS naming: from a series as the scanner names it to its BIDS name (used by braims.py on BIDScoin's bidsmap).

Matching ignores accents and case (ÓRBITA = orbita). The first rule of naming_rules.csv that matches wins.
Spine series are recognised before the rules (by name, by the DICOM body part, by the exam description and by the
field of view) and named acq-<region><plane><contrast>; they are held at the site until spine defacing exists.
"""
import csv, re, unicodedata
from pathlib import Path

PARTS = ["", "mag", "phase", "real", "imag"]
POST = re.compile(r"(^|[^a-z])(post|pos|postcontrast|postcontraste|poscontraste|gd|gado|gadolinio|gadolinium|"
                  r"contrast|contraste|contrastado|ce|c\+|\+c|\+gd)([^a-z]|$)")
NOT_POST = re.compile(r"(^|[^a-z])(pre|sin|sem|without|no)[ _-]?(contrast|contraste|gd|gado)")
SPINE = re.compile(r"colun|column|medul|(^|[^a-z])med([^a-z]|$)|spine|spinal|cspine|tspine|lspine|cervic|"
                   r"(^|[^a-z])dorsal|torac|thorac|lumb|lombar|(^|[^a-z])col([^a-z]|$)|raqui|(^|[^a-z])c[1-7](-c?[1-7])?([^a-z0-9]|$)")
REGIONS = (("cspine", re.compile(r"cerv|c-?spine|cspine|(^|[^a-z])c[1-7]([^0-9]|$)")),
           ("tspine", re.compile(r"torac|thorac|dorsal|t-?spine|tspine")),
           ("lspine", re.compile(r"lumb|lombar|l-?spine|lspine|sacr")))
BODY_PART = {"CSPINE": "cspine", "TSPINE": "tspine", "LSPINE": "lspine", "SSPINE": "lspine", "SPINE": "spine",
             "CTSPINE": "spine", "TLSPINE": "spine", "WHOLESPINE": "spine", "NECK": "cspine"}
CONTRAST_TAG = (("stir", "STIR"), ("psir", "PSIR"), ("mprage", "MPRAGE"), ("flair", "FLAIR"), ("pd", "PD"),
                ("t2*", "T2star"), ("t2star", "T2star"), ("gre", "T2star"), ("t1", "T1"), ("t2", "T2"))


def plain(text):
    """Lower case, accents removed: ÓRBITA -> orbita, pós -> pos."""
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def rules(*files):
    """Rules from the CSV files, first file first (local rules win)."""
    out = []
    for f in files:
        if Path(f).exists():
            with open(f, encoding="utf-8") as h:
                out += [r for r in csv.DictReader(h) if r.get("pattern")]
    return out


def match(table, desc, mr):
    text = plain(desc)
    return next((r for r in table if re.search(r["pattern"], text) and (not r["mr"] or r["mr"] == mr)), None)


def is_post(desc, attributes=None):
    """Contrast-enhanced: from the DICOM ContrastBolusAgent when the scanner filled it, else from the name."""
    agent = str((attributes or {}).get("ContrastBolusAgent") or "").strip()
    if agent and plain(agent) not in ("none", "no", "n/a", "0"):
        return True
    text = re.sub(r"[_\-\s]+", " ", plain(desc))
    return bool(POST.search(text)) and not NOT_POST.search(text)


def dicom_hint(path):
    """What the DICOM header of one file of the series says about the body part and the field of view, or {}."""
    try:
        import pydicom
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception:                                           # noqa: BLE001 - a hint only
        return {}
    hint = {k: str(getattr(ds, k, "") or "") for k in
            ("BodyPartExamined", "StudyDescription", "RequestedProcedureDescription", "ContrastBolusAgent")}
    try:
        iop = [float(x) for x in ds.ImageOrientationPatient]
        normal = [abs(iop[1] * iop[5] - iop[2] * iop[4]), abs(iop[2] * iop[3] - iop[0] * iop[5]),
                  abs(iop[0] * iop[4] - iop[1] * iop[3])]
        hint["plane"] = ("sag", "cor", "ax")[normal.index(max(normal))]
        hint["fov_mm"] = max(float(ds.Rows) * float(ds.PixelSpacing[0]), float(ds.Columns) * float(ds.PixelSpacing[1]))
    except Exception:                                           # noqa: BLE001
        pass
    return hint


def spine_region(desc, hint=None):
    """(region, how) when the series is of the spine: region cspine / tspine / lspine / spine; (None, "") otherwise;
    ("possible", how) when only the field of view suggests it (held for a person to decide)."""
    hint = hint or {}
    text = plain(desc)
    body = re.sub(r"[^A-Z]", "", str(hint.get("BodyPartExamined", "")).upper())
    exam = plain(f"{hint.get('StudyDescription', '')} {hint.get('RequestedProcedureDescription', '')}")
    how = None
    if SPINE.search(text):
        how = "series name"
    elif body in BODY_PART:
        how = f"DICOM body part {body}"
    elif SPINE.search(exam) and not re.search(r"cranio|craneo|cerebr|brain|encefal|head|orbit", exam):
        how = "exam description"
    if how:
        for region, pattern in REGIONS:
            if pattern.search(text) or pattern.search(exam):
                return region, how
        return BODY_PART.get(body, "spine"), how
    if hint.get("plane") == "sag" and float(hint.get("fov_mm") or 0) >= 300:
        return "possible", "a sagittal field of view of 30 cm or more"
    return None, ""


def plane_tag(desc, hint=None):
    text = plain(desc)
    for word, tag in (("sag", "Sag"), ("ax", "Ax"), ("tra", "Ax"), ("cor", "Cor")):
        if re.search(rf"(^|[^a-z]){word}", text):
            return tag
    return {"sag": "Sag", "ax": "Ax", "cor": "Cor"}.get((hint or {}).get("plane"), "")


def contrast_tag(desc):
    text = plain(desc)
    for word, tag in CONTRAST_TAG:
        if word in text:
            return tag
    return ""


def bids_entry(datatype, rule, desc, acq, ce):
    part = PARTS + [PARTS.index(rule["part"]) if rule and rule.get("part") else 0]
    if datatype == "dwi":
        return {"acq": acq, "rec": None, "dir": "", "run": "<<>>", "part": part, "chunk": None, "suffix": "dwi"}
    return {"task": None, "acq": acq, "ce": ce, "rec": None, "run": "<<>>", "echo": None, "part": part,
            "chunk": None, "suffix": rule["suffix"] if rule else "T2w"}


def name_run(run, datatype, table, hint=None):
    """Name one bidsmap run in place. Returns (new datatype, note) - datatype 'exclude' when it is not converted."""
    a = run.get("attributes", {})
    desc = str(a.get("SeriesDescription") or a.get("ProtocolName") or "")
    mr = str(a.get("MRAcquisitionType") or "")
    hint = hint or {}
    rule = match(table, desc, mr)
    post = is_post(desc, {**hint, **a})
    if rule and rule["datatype"] == "exclude":
        return "exclude", rule["why"]
    region, how = spine_region(desc, hint)
    if region:
        if not isinstance(run.get("meta"), dict):
            run["meta"] = {}
        meta = run["meta"]
        if region == "possible":
            meta["BRAIMSRegion"] = "possible spine"
            meta["BRAIMSHold"] = f"possibly spine ({how}): a person decides"
        else:
            meta["BRAIMSRegion"] = "spine"
            meta["BodyPart"] = {"cspine": "CSPINE", "tspine": "TSPINE", "lspine": "LSPINE"}.get(region, "SPINE")
            meta["BRAIMSHold"] = "spine: kept at the site until spine defacing is available"
        if region != "possible":
            acq = f"{region}{plane_tag(desc, hint)}{contrast_tag(desc)}"
            suffix_rule = rule if rule and rule["datatype"] == "anat" else {"suffix": "T2w", "part": ""}
            run["bids"] = bids_entry("anat", suffix_rule, desc, acq, "gadolinium" if post else None)
            return "anat", f"spine ({how}): held at the site until spine defacing is available"
    if rule:
        new = rule["datatype"]
        acq = re.sub(r"[^A-Za-z0-9]", "", desc) if rule["acq"] == "=desc" else rule["acq"]
        ce = "gadolinium" if rule["ce"] == "auto" and post else None
        if new == "dwi" and datatype == "dwi":                  # keep BIDScoin's diffusion entry (it knows the PE direction)
            run["bids"]["acq"] = acq
        else:
            run["bids"] = bids_entry(new, rule, desc, acq, ce)
        if region == "possible":
            return new, f"{rule['why']}; POSSIBLY SPINE ({how}): held until a person decides"
        return new, rule["why"]
    if datatype == "extra_data":
        return "exclude", "NOT RECOGNIZED, so not converted. Send this series name to the core center."
    return datatype, "no BRAIMS rule: BIDScoin's own name kept. Check it."


def result_name(new, run):
    """The BIDS name a run will get, for the report."""
    if new == "exclude":
        return "excluded"
    b = run["bids"]
    parts = [f"{k}-{v}" for k, v in b.items() if k in ("acq", "ce") and v]
    if isinstance(b.get("part"), list) and b["part"][-1]:
        parts.append(f"part-{b['part'][b['part'][-1]]}")
    return f"{new}/" + "_".join(parts + [b["suffix"]])
