#!/usr/bin/env python3
"""BRAIMS check: is this BIDS folder (made with BIDScoin) safe to leave the hospital?

    python braims_check.py <bids_folder>                   report only, changes nothing
    python braims_check.py <bids_folder> --fix             also removes what must not leave
    python braims_check.py <bids_folder> --package <zip>   if clean, build the upload package

Needs only Python 3.9 or newer (the same Python used for BIDScoin).
Exit code 0 = no errors; 1 = errors to fix before upload.
"""
import csv, gzip, hashlib, json, re, struct, sys, zipfile
from pathlib import Path

# Sidecar fields that never leave the site: they point to the person, the date or the hospital.
REMOVE = {
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex", "PatientAge", "PatientWeight",
    "PatientSize", "PatientComments", "AccessionNumber", "StudyID", "StudyInstanceUID",
    "SeriesInstanceUID", "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
    "StationName", "DeviceSerialNumber", "ReferringPhysicianName", "PerformingPhysicianName",
    "OperatorsName", "RequestingPhysician", "AcquisitionDateTime", "AcquisitionDate", "StudyDate",
    "SeriesDate", "ContentDate", "StudyDescription", "ProcedureStepDescription", "ImageComments",
}
LEAVE_OUT = ("code", "derivatives", "sourcedata")       # folders that stay at the site
KEEP_COLUMN = re.compile(r"^(participant_id|sex|age(@ses-[A-Za-z0-9]+)?)$")
LOCAL_CODE = re.compile(r"^[A-Z]{2}\d{6}$")             # country + 2-digit site + 4-digit number
ANON_DATE = "1925-01-01"                                # BIDScoin's placeholder date
DATE = re.compile(r"\b(19|20)\d{2}[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])\b"
                  r"|\b(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](19|20)\d{2}\b")
NOT_A_DATE = {"ConversionSoftwareVersion", "SoftwareVersions", "ConsistencyInfo", "WipMemBlock"}
SUFFIXES = {"T1w", "T2w", "FLAIR", "PDw", "PDT2", "T2starw", "T2star", "T1map", "T2map", "UNIT1", "MP2RAGE", "MEGRE", "MESE",
            "inplaneT1", "inplaneT2", "angio", "dwi", "sbref", "bold", "epi", "magnitude", "magnitude1", "magnitude2",
            "phasediff", "phase1", "phase2", "fieldmap", "asl", "m0scan"}
CONVERTER_COPY = re.compile(r"(Eq\d+|Tilt\d*|reala|imaginarya|pha)$")    # dcm2niix variants: resampled or second copies
found = {}      # (level, message) -> files


def image_dims(f):
    with (gzip.open(f) if f.name.endswith(".gz") else open(f, "rb")) as h:
        dims = struct.unpack("<8h", h.read(348)[40:56])
    return dims[1:dims[0] + 1]


def report(level, path, msg):
    found.setdefault((level, msg), []).append(str(path))


def show(level):
    for (lv, msg), paths in found.items():
        if lv == level:
            print(f"{level:8}{len(paths)} file(s): {msg}")
            for p in paths[:3]:
                print(f"          {p}")
            if len(paths) > 3:
                print(f"          ... and {len(paths) - 3} more")
    return sum(len(p) for (lv, _), p in found.items() if lv == level)


def files(bids, pattern):
    return [f for f in sorted(bids.rglob(pattern)) if f.relative_to(bids).parts[0] not in LEAVE_OUT]


def check_names(bids):
    for folder in LEAVE_OUT:
        if (bids / folder).exists():
            print(f"NOTE    {folder}/ stays at the site (the upload package leaves it out)")
    for sub in sorted(p for p in bids.glob("sub-*") if p.is_dir()):
        if not LOCAL_CODE.match(sub.name[4:]):
            report("ERROR", sub.name, "the code must be country + site + number, for example sub-BR040012")
    for f in files(bids, "*"):
        if f.suffix.lower() in (".dcm", ".ima") or f.name.upper() == "DICOMDIR":
            report("ERROR", f.relative_to(bids), "DICOM file; DICOM never leaves the site")
    for f in files(bids, "*.nii*"):
        rel, dims = f.relative_to(bids), image_dims(f)
        parts = f.name.split(".")[0].split("_")
        side = f.parent / (f.name.split(".")[0] + ".json")
        meta = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        if parts[-1] not in SUFFIXES or any(CONVERTER_COPY.search(p.split("-", 1)[-1]) for p in parts[1:-1]):
            report("ERROR", f"{rel}  ({'x'.join(map(str, dims))})",
                   "extra copy made by the converter: the exam folder held two versions of this series, or slices were missing. "
                   "Keep the complete image (compare the sizes), delete the others with their .json/.bval/.bvec, or export the exam again")
        if len(dims) < 3 or dims[2] < 2:
            report("ERROR", f"{rel}  ({'x'.join(map(str, dims))})",
                   "one slice only: a scanner mosaic that the converter could not unpack, because the export lost the scanner's "
                   "private header. Export the series again with all DICOM tags kept, or delete it with its .json/.bval/.bvec")
        elif meta.get("Defaced") is not True:
            report("ERROR", rel, "not defaced: run the defacing step. An image it cannot deface stays at the site: "
                                 "delete it with its .json/.bval/.bvec")


def check_json(bids, fix):
    for f in files(bids, "*.json"):
        rel = f.relative_to(bids)
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        extra = sorted(k for k in data if k in REMOVE)
        if f.name == "participants.json":
            extra = sorted(k for k in data if not KEEP_COLUMN.match(k))
        if extra and fix:
            for k in extra:
                del data[k]
            f.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
            report("FIXED", rel, "removed " + ", ".join(extra))
        elif extra:
            report("ERROR", rel, "contains " + ", ".join(extra))
        for k, v in data.items():
            if k not in NOT_A_DATE and isinstance(v, str) and DATE.search(v):
                report("WARNING", f"{rel}  {k} = {v!r}", "a value looks like a date")


def check_tsv(bids, fix):
    for f in files(bids, "*.tsv"):
        rel = f.relative_to(bids)
        with f.open(newline="", encoding="utf-8") as h:
            rows = list(csv.reader(h, delimiter="\t"))
        if not rows:
            continue
        head, changed = rows[0], []
        if f.name == "participants.tsv":
            drop = [i for i, c in enumerate(head) if not KEEP_COLUMN.match(c)]
            if drop and fix:
                changed.append("removed columns " + ", ".join(head[i] for i in drop))
                rows = [[c for i, c in enumerate(r) if i not in drop] for r in rows]
                head = rows[0]
            elif drop:
                report("ERROR", rel, "contains the columns " + ", ".join(head[i] for i in drop))
            for i, c in enumerate(head):
                for r in rows[1:]:
                    if c.startswith("age") and r[i].replace(".", "", 1).isdigit() and float(r[i]) > 89:
                        if fix:
                            r[i] = "89"
                            changed.append(f"{r[0]}: age capped at 89")
                        else:
                            report("ERROR", f"{rel}  {r[0]}", "age above 89 (must be written as 89)")
        if f.name.endswith("_scans.tsv") and "acq_time" in head:
            i = head.index("acq_time")
            for r in rows[1:]:
                if r[i] not in ("", "n/a") and not r[i].startswith(ANON_DATE):
                    if fix:
                        r[i] = ANON_DATE + r[i][10:]
                        changed.append(f"{r[0]}: scan date replaced")
                    else:
                        report("ERROR", f"{rel}  {r[0]}", "real scan date in acq_time")
        if changed:
            with f.open("w", newline="", encoding="utf-8") as h:
                csv.writer(h, delimiter="\t", lineterminator="\n").writerows(rows)
            report("FIXED", f"{rel}  ({'; '.join(sorted(set(changed)))})", "table corrected")
        for r in rows[1:]:
            for cell in r:
                if DATE.search(cell) and not cell.startswith(ANON_DATE):
                    report("WARNING", f"{rel}  {cell!r}", "a value looks like a date")


def check_nifti(bids, fix):
    # NIfTI-1 header text fields: db_name (18 bytes at 14), descrip (80 at 148), aux_file (24 at 228)
    for f in files(bids, "*.nii") + files(bids, "*.nii.gz"):
        rel = f.relative_to(bids)
        opener = gzip.open if f.name.endswith(".gz") else open
        with opener(f, "rb") as h:
            head = h.read(352)
        if len(head) < 348 or struct.unpack("<i", head[:4])[0] != 348:
            continue                                    # not a little-endian NIfTI-1 file
        text = lambda a, n: head[a:a + n].split(b"\0")[0].decode("latin-1").strip()
        left = {name: text(a, n) for name, a, n in (("db_name", 14, 18), ("aux_file", 228, 24)) if text(a, n)}
        if DATE.search(text(148, 80)):
            report("WARNING", f"{rel}  {text(148, 80)!r}", "the image header description looks like it holds a date")
        if left and fix:
            with opener(f, "rb") as h:
                data = bytearray(h.read())
            data[14:32], data[228:252] = bytes(18), bytes(24)
            with opener(f, "wb") as h:
                h.write(data)
            report("FIXED", f"{rel}  {list(left.values())}", "cleared free text in the image header")
        elif left:
            report("ERROR", f"{rel}  {list(left.values())}", "free text in the image header")


def package(bids, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    items = [f for f in files(bids, "*") if f.is_file()]
    lines = ["file\tbytes\tsha256"]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in items:
            rel = f.relative_to(bids).as_posix()
            data = f.read_bytes()
            z.write(f, rel)
            lines.append(f"{rel}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}")
        z.writestr("manifest.tsv", "\n".join(lines) + "\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    Path(str(target) + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(f"\nPackage: {target}  ({len(items)} files, {target.stat().st_size / 1e6:.1f} MB)")
    print(f"SHA-256: {digest}")


def main():
    args = sys.argv[1:]
    fix, target = "--fix" in args, None
    if "--package" in args:
        target = args[args.index("--package") + 1]
        args.remove(target)
    args = [a for a in args if not a.startswith("--")]
    if len(args) != 1 or not Path(args[0]).is_dir():
        sys.exit(__doc__)
    bids = Path(args[0]).resolve()
    print(f"BRAIMS check of {bids}\n")
    check_names(bids)
    check_json(bids, fix)
    check_tsv(bids, fix)
    check_nifti(bids, fix)
    show("FIXED")
    errors, warnings = show("ERROR"), show("WARNING")
    print(f"\n{errors} error(s), {warnings} warning(s).")
    if errors:
        print("Run again with --fix, then check again. Nothing is uploaded while errors remain.")
    elif warnings:
        print("No errors. A person must look at each warning before upload.")
    else:
        print("Nothing found. A second person still checks before upload.")
    if target and not errors:
        package(bids, target)
    elif target:
        print("No package was made because errors remain.")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
