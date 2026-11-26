"""Information kept for analysis without identifying anyone, and checks made before anything runs.

scanner IDs     BRAIMSScannerID = a salted hash of the scanner's serial number and station name: the same scanner always
                gets the same ID (for harmonization), but the ID does not reveal the scanner. The salt stays at the site.
sessions.tsv    days_from_baseline for each session, from the DICOM study dates (the dates themselves never leave).
pre-flight      exam folders named as BRAIMS codes, one person per session, enough free disk space.
"""
import datetime, hashlib, json, os, re, secrets, shutil
from pathlib import Path

VERSION = "1.1.0"
CODE = re.compile(r"^sub-[A-Z]{2}\d{6}$")
SESSION = re.compile(r"^ses-\d{2}$")


def dicom_files(folder, per_folder=1, limit=400):
    """A few DICOM files of an exam: the first file of each folder."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        taken = 0
        for name in sorted(files):
            p = Path(root) / name
            try:
                with open(p, "rb") as h:
                    if h.read(132)[128:132] != b"DICM" and not name.lower().endswith((".dcm", ".ima")):
                        continue
            except OSError:
                continue
            out.append(p)
            taken += 1
            if taken >= per_folder:
                break
        if len(out) >= limit:
            break
    return out


def read_tags(path, *tags):
    try:
        import pydicom
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True, specific_tags=list(tags))
        return {t: str(getattr(ds, t, "") or "").strip() for t in tags}
    except Exception:                                           # noqa: BLE001
        return {t: "" for t in tags}


def preflight(raw, data):
    """Problems that must be solved before anything runs: [(where, what)]."""
    problems = []
    if not raw.is_dir():
        return [(str(raw), "no raw folder: put each exam in raw/sub-<code>/ses-<nn> first")]
    for sub in sorted(p for p in raw.iterdir() if not p.name.startswith(".")):
        if not sub.is_dir():
            problems.append((sub.name, "a file directly in raw/: each exam goes in raw/sub-<code>/ses-<nn>/"))
            continue
        if not CODE.match(sub.name):
            problems.append((sub.name, "not a BRAIMS code: sub- + 2 capital letters + 6 digits, for example sub-BR010001"))
            continue
        sessions = [p for p in sub.iterdir() if p.is_dir()]
        if not sessions:
            problems.append((sub.name, "no session folder inside: use ses-01, ses-02, ..."))
        for ses in sessions:
            if not SESSION.match(ses.name):
                problems.append((f"{sub.name}/{ses.name}", "not a session name: use ses-01, ses-02, ..."))
                continue
            people = {}
            for f in dicom_files(ses):
                t = read_tags(f, "PatientID", "PatientName", "PatientBirthDate")
                who = t["PatientID"] or t["PatientName"]
                if who:
                    people.setdefault(who, f.parent.name)
            if len(people) > 1:
                problems.append((f"{sub.name}/{ses.name}", f"{len(people)} different patients in one exam folder "
                                 f"(series {', '.join(sorted(people.values()))}): keep one person per folder"))
    size = sum(f.stat().st_size for f in raw.rglob("*") if f.is_file())
    free = shutil.disk_usage(data).free
    if free < 3 * size:
        problems.append((str(data), f"not enough free disk space: {free / 1e9:.1f} GB free, about {3 * size / 1e9:.1f} GB "
                                    "needed (three times the size of raw/)"))
    return problems


def study_date(ses_folder):
    for f in dicom_files(ses_folder, limit=20):
        t = read_tags(f, "StudyDate", "SeriesDate", "AcquisitionDate")
        for k in ("StudyDate", "SeriesDate", "AcquisitionDate"):
            try:
                return datetime.datetime.strptime(t[k][:8], "%Y%m%d").date()
            except ValueError:
                continue
    return None


def write_sessions(bids, raw):
    """sub-X/sub-X_sessions.tsv with days_from_baseline from the DICOM study dates (only the difference leaves)."""
    written = []
    for sub in sorted(p for p in bids.glob("sub-*") if p.is_dir()):
        sessions = sorted(p.name for p in sub.glob("ses-*") if p.is_dir())
        dates = {s: study_date(raw / sub.name / s) for s in sessions}
        known = [d for d in dates.values() if d]
        base = min(known) if known else None
        rows = ["session_id\tdays_from_baseline"]
        for s in sessions:
            rows.append(f"{s}\t{(dates[s] - base).days if dates[s] and base else 'n/a'}")
        (sub / f"{sub.name}_sessions.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        written.append(sub.name)
    return written


def salt(bids):
    p = bids / "code" / "braims" / "scanner_salt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(16))
    return p.read_text().strip()


def scanner_ids(bids):
    """BRAIMSScannerID and BRAIMSVersion in every sidecar that still has the scanner's identity (before the check removes it)."""
    s, n = salt(bids), 0
    for f in sorted(bids.glob("sub-*/ses-*/*/*.json")):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(meta, dict):
            continue
        changed = False
        if "BRAIMSScannerID" not in meta:
            ident = "|".join(str(meta.get(k, "")) for k in ("DeviceSerialNumber", "StationName", "InstitutionName",
                                                            "Manufacturer", "ManufacturersModelName"))
            if ident.strip("|"):
                meta["BRAIMSScannerID"] = hashlib.sha256((s + ident).encode()).hexdigest()[:10]
                changed = True
        if meta.get("BRAIMSVersion") != VERSION:
            meta["BRAIMSVersion"] = VERSION
            changed = True
        if changed:
            f.write_text(json.dumps(meta, indent=4, ensure_ascii=False), encoding="utf-8")
            n += 1
    return n


def provenance(bids):
    """GeneratedBy in dataset_description.json."""
    p = bids / "dataset_description.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"Name": "BRAIMS", "BIDSVersion": "1.9.0"}
    except ValueError:
        d = {"Name": "BRAIMS", "BIDSVersion": "1.9.0"}
    gen = [g for g in d.get("GeneratedBy", []) if isinstance(g, dict) and g.get("Name") != "BRAIMS"]
    d["GeneratedBy"] = gen + [{"Name": "BRAIMS", "Version": VERSION,
                               "CodeURL": "https://github.com/jonadabsilva/braims-imaging"}]
    p.write_text(json.dumps(d, indent=4, ensure_ascii=False), encoding="utf-8")


def raw_fingerprint(ses_folder):
    h = hashlib.sha256()
    for f in sorted(ses_folder.rglob("*")):
        if f.is_file():
            h.update(f"{f.relative_to(ses_folder).as_posix()}|{f.stat().st_size}".encode())
    return h.hexdigest()


def stale_sessions(raw, bids):
    """Sessions converted before but interrupted, or whose raw exam changed since: they are converted again."""
    done_dir = bids / "code" / "braims" / "converted"
    if not done_dir.exists():                                   # first run of this version on an existing folder: what is
        mark_converted(raw, bids)                               # there was converted before (masks edited by hand stay)
        return []
    out = []
    for ses in sorted(bids.glob("sub-*/ses-*")):
        if not ses.is_dir():
            continue
        marker = done_dir / f"{ses.parent.name}_{ses.name}"
        src = raw / ses.parent.name / ses.name
        if not src.is_dir():
            continue
        if not marker.exists() or marker.read_text().strip() != raw_fingerprint(src):
            out.append(ses)
    return out


def forget(ses, bids):
    """Remove a session's converted and defaced files so that it is converted again from raw."""
    rel = ses.relative_to(bids)
    for p in (ses, bids / "derivatives" / "braims_deface" / rel, bids / "derivatives" / "braims_deface" / "orig" / rel):
        shutil.rmtree(p, ignore_errors=True)


def mark_converted(raw, bids):
    done_dir = bids / "code" / "braims" / "converted"
    done_dir.mkdir(parents=True, exist_ok=True)
    for ses in sorted(bids.glob("sub-*/ses-*")):
        src = raw / ses.parent.name / ses.name
        if ses.is_dir() and src.is_dir():
            (done_dir / f"{ses.parent.name}_{ses.name}").write_text(raw_fingerprint(src))
