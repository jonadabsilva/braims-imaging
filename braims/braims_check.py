#!/usr/bin/env python3
"""BRAIMS check: is this BIDS folder (made with BIDScoin) safe to leave the hospital?

    python braims_check.py <bids_folder>                   report only, changes nothing
    python braims_check.py <bids_folder> --fix             also removes what must not leave
    python braims_check.py <bids_folder> --package <zip>   if clean, build the upload package

Needs only Python 3.9 or newer (the same Python used for BIDScoin).
Exit code 0 = no errors; 1 = errors to fix before upload.

What may leave is decided by lists of what is allowed, not of what is forbidden:
  files     only BIDS files of the kinds BRAIMS makes (images, sidecars, tables); anything else is an error
  sidecars  only known acquisition keys; numbers under other technical names; everything else is removed by --fix
  images    held images (BRAIMSHold in the sidecar) stay at the site and are left out of the package
"""
import csv, gzip, hashlib, io, json, re, struct, sys, zipfile, zlib
from pathlib import Path

VERSION = "1.1.0"
LEAVE_OUT = ("code", "derivatives", "sourcedata")       # folders that stay at the site
KEEP_COLUMN = re.compile(r"^(participant_id|sex|age(@ses-[A-Za-z0-9]+)?)$")
SESSION_COLUMN = re.compile(r"^(session_id|days_from_baseline|age)$")
SCANS_COLUMN = re.compile(r"^(filename|acq_time)$")
LOCAL_CODE = re.compile(r"^[A-Z]{2}\d{6}$")             # country + 2-digit site + 4-digit number
ANON_DATE = "1925-01-01"                                # BIDScoin's placeholder date
DATE = re.compile(r"\b(19|20)\d{2}[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])\b"
                  r"|\b(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](19|20)\d{2}\b")
LONG_NUMBER = re.compile(r"\d{5,}")                     # record numbers, phone numbers, IDs in free text
SUFFIXES = {"T1w", "T2w", "FLAIR", "PDw", "PDT2", "T2starw", "T2star", "T1map", "T2map", "UNIT1", "MP2RAGE", "MEGRE", "MESE",
            "inplaneT1", "inplaneT2", "angio", "dwi", "sbref", "bold", "epi", "magnitude", "magnitude1", "magnitude2",
            "phasediff", "phase1", "phase2", "fieldmap", "asl", "m0scan"}
CONVERTER_COPY = re.compile(r"(Eq\d+|Tilt\d*|reala|imaginarya|pha)$")    # dcm2niix variants: resampled or second copies

# Sidecar keys that may leave (text or numbers). Everything here is acquisition physics or BRAIMS bookkeeping.
ALLOWED_KEYS = {
    # scanner and sequence
    "Modality", "MagneticFieldStrength", "ImagingFrequency", "Manufacturer", "ManufacturersModelName", "SoftwareVersions",
    "MRAcquisitionType", "ScanningSequence", "SequenceVariant", "ScanOptions", "SequenceName", "PulseSequenceName",
    "PulseSequenceDetails", "InternalPulseSequenceName", "PulseSequenceType", "ImageType", "ImageTypeText", "RawImage",
    "NonlinearGradientCorrection", "SeriesNumber", "AcquisitionNumber", "AcquisitionTime", "SeriesDescription",
    "ProtocolName", "BodyPart", "BodyPartDetails", "BodyPartExamined", "PatientPosition",
    # timing and contrast
    "EchoTime", "EchoTime1", "EchoTime2", "EchoNumber", "EchoTrainLength", "RepetitionTime", "RepetitionTimeExcitation",
    "RepetitionTimePreparation", "InversionTime", "FlipAngle", "SAR", "MTState", "SpoilingState", "SpoilingType",
    "SpoilingRFPhaseIncrement", "SpectrallySelectedSuppression", "FatSuppression", "ContrastBolusAgent",
    "ContrastBolusIngredient", "AcquisitionDuration", "NumberOfAverages", "VariableFlipAngleFlag",
    # geometry
    "SliceThickness", "SpacingBetweenSlices", "SliceTiming", "SliceEncodingDirection", "ImageOrientationPatientDICOM",
    "ImageOrientationText", "InPlanePhaseEncodingDirectionDICOM", "PhaseEncodingDirection", "PhaseEncodingAxis",
    "AcquisitionMatrixPE", "ReconMatrixPE", "BaseResolution", "PhaseResolution", "PercentPhaseFOV", "PercentSampling",
    "PhaseEncodingSteps", "FrequencyEncodingSteps", "PhaseOversampling", "PartialFourier", "PartialFourierDirection",
    "AcquisitionVoxelSize",
    # coils and acceleration
    "ReceiveCoilName", "ReceiveCoilActiveElements", "CoilString", "CoilCombinationMethod", "MatrixCoilMode",
    "TransmitCoilName", "ParallelReductionFactorInPlane", "ParallelReductionFactorOutOfPlane",
    "ParallelAcquisitionTechnique", "MultibandAccelerationFactor", "RefLinesPE",
    # readout
    "PixelBandwidth", "DwellTime", "EffectiveEchoSpacing", "TotalReadoutTime", "DerivedVendorReportedEchoSpacing",
    "BandwidthPerPixelPhaseEncode", "WaterFatShift", "ShimSetting", "TxRefAmp", "B0FieldIdentifier", "B0FieldSource",
    # vendor scaling
    "PhilipsRescaleSlope", "PhilipsRescaleIntercept", "PhilipsScaleSlope", "PhilipsRWVSlope", "PhilipsRWVIntercept",
    "UsePhilipsFloatNotDisplayScaling",
    # diffusion
    "DiffusionScheme", "IntendedFor", "Units",
    # conversion
    "ConversionSoftware", "ConversionSoftwareVersion", "Dcm2bidsVersion", "BidsGuess",
    # BRAIMS
    "Defaced", "NeckRemoved", "DefacingMethod", "DefacingNote", "ImageRepair", "BRAIMSHold", "BRAIMSScannerID",
    "BRAIMSVersion", "BRAIMSRegion", "BRAIMSDefaceVersion",
}
DESCRIPTIVE_KEYS = {"SeriesDescription", "ProtocolName", "BodyPartDetails"}      # typed by people at the scanner
# Words that may stay in descriptive text (series names); any other word of 3+ letters is masked, so that a name typed
# into a series description cannot leave. Accents are removed before comparing.
VOCABULARY = set("""
t1 t2 t1w t2w pd pdw dp flair stir psir dir swi swan gre fgre spgr fspgr bravo mprage mp2rage uni inv inv1 inv2 mpr tfe ffe
tse fse se ir irfse frfse haste space spc cube vista vibe lava starvibe tirm dark fluid darkfluid da fl tra ax axi axial sag
sagital sagittal cor coronal obl oblique iso tilt 2d 3d mip minip mag pha phase real imag images image dwi dti diff diffusion
ep2d epi resolve adc fa trace tracew b0 b1000 mddw dir dirs fat sat fs fatsat water dixon opp inp in out post pos pre
gado gadolinio gadolinium contrast contraste contrastado com sin con sem gd mt mton mtoff tof angio mra mrv pc perf perfusion
dsc dce asl pcasl pasl bold rest fmri cbf brain cerebro encefalo encefalico cranio craneo cranial head cabeza cabeca neuro
orbit orbits orbita orbitas optic optico opticos nervio nervios nervo nervos no nn oo spine spinal cspine tspine lspine
coluna columna medula cervical toracica toracico dorsal lumbar lombar sacra sacral cord whole total composed comp
hemo hemosiderin star t2star susc susceptibility qsm swi_images high res hr fast turbo short long thin thick slab multi
slice slices echo dual double triple single shot multishot nd norm normalized dis2d dis3d distortion corrected moco
localizer loc scout survey calibration asset sense grappa caipi acc accel cs compressed sensing repeat rep run copy
ref sbref fieldmap gre_field map mapping t1map t2map r2 r2star pdmap qmap mpm synthetic synthmr magic
view shc vol volume left right bilateral esq dir direita izquierda derecha esquerda fino finos
""".split())
WORD = re.compile(r"[A-Za-z]{3,}")


def mask_words(text):
    """Mask every word of 3+ letters that is not an MRI term (names typed into series descriptions)."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return WORD.sub(lambda m: m.group(0) if m.group(0).lower() in VOCABULARY else "*", plain)


# a number under any other name may leave, unless the name points to the person, a date or an identifier
NUMBER_OK = re.compile(r"^[A-Z][A-Za-z0-9]*$")
NUMBER_NOT_OK = re.compile(r"(?i)patient|birth|date|age$|weight|size$|accession|uid|identifier|serial|^id$|id$|station|"
                           r"institution|physician|operator|address|phone|name$")
JUNK = re.compile(r"^(\.DS_Store|Thumbs\.db|desktop\.ini|\._.*)$", re.I)

# Files that may leave, relative to the BIDS folder
SUB, SES = r"sub-[A-Za-z0-9]+", r"ses-[A-Za-z0-9]+"
ALLOWED_FILES = re.compile("|".join([
    r"dataset_description\.json", r"README(\.(md|txt|rst))?", r"CHANGES", r"\.bidsignore",
    r"participants\.(tsv|json)",
    rf"{SUB}/{SUB}_sessions\.tsv",
    rf"{SUB}/{SES}/{SUB}_{SES}_scans\.(tsv|json)",
    rf"{SUB}/{SES}/(anat|dwi)/{SUB}_{SES}(_[a-zA-Z]+-[a-zA-Z0-9]+)*_[a-zA-Z0-9]+\.(nii\.gz|json|bval|bvec)",
]) .join(("^(", ")$")))

found = {}      # (level, message) -> files


def report(level, path, msg):
    found.setdefault((level, msg), []).append(str(path))


def show(level):
    for (lv, msg), paths in found.items():
        if lv == level:
            print(f"{level:8}{len(paths)} file(s): {msg}")
            for p in paths[:50]:
                print(f"          {p}")
            if len(paths) > 50:
                print(f"          ... and {len(paths) - 50} more")
    return sum(len(p) for (lv, _), p in found.items() if lv == level)


# ----------------------------------------------------------------------------------------------- reading, never crashing

def read_text(f):
    """Text of a file whatever the encoding Excel or a hospital PC saved it in. Returns (text, was_clean_utf8)."""
    raw = f.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            return text.lstrip("\ufeff"), enc == "utf-8" and not text.startswith("\ufeff")
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), False


def read_json(f):
    text, _ = read_text(f)
    return json.loads(text)


def sidecar_of(img):
    side = img.parent / (img.name.split(".")[0] + ".json")
    try:
        return side, (read_json(side) if side.exists() else {})
    except (ValueError, OSError):
        return side, {}


def held(img):
    meta = sidecar_of(img)[1]
    return isinstance(meta, dict) and bool(meta.get("BRAIMSHold"))


def image_header(f):
    """Image size and voxel size (mm) from the NIfTI header."""
    with (gzip.open(f) if f.name.endswith(".gz") else open(f, "rb")) as h:
        header = h.read(348)
    if len(header) < 348:
        raise ValueError("file too short for a NIfTI header")
    dims, pixdims = struct.unpack("<8h", header[40:56]), struct.unpack("<8f", header[76:108])
    if not 1 <= dims[0] <= 7:
        raise ValueError("not a little-endian NIfTI-1 file")
    return dims[1:dims[0] + 1], pixdims[1:dims[0] + 1]


def is_dicom(f):
    try:
        with open(f, "rb") as h:
            return h.read(132)[128:132] == b"DICM"
    except OSError:
        return False


def image_complete(f, cache):
    """The whole .nii.gz decompresses (CRC checked) and holds as many bytes as its header says."""
    stat = f.stat()
    key = f"{f.as_posix()}|{stat.st_size}|{int(stat.st_mtime)}"
    if cache.get(key):
        return True
    with open(f, "rb") as h:
        header = gzip.GzipFile(fileobj=h).read(352)
    dims = struct.unpack("<8h", header[40:56])
    bitpix = struct.unpack("<h", header[72:74])[0]
    offset = int(struct.unpack("<f", header[108:112])[0])
    expected = offset + (bitpix // 8) * int(__import__("math").prod(max(1, d) for d in dims[1:dims[0] + 1]))
    total, z = 0, zlib.decompressobj(16 + zlib.MAX_WBITS)
    with open(f, "rb") as h:
        while chunk := h.read(1 << 20):
            total += len(z.decompress(chunk))
            while z.unused_data:                       # a gzip file may hold several members
                rest = z.unused_data
                z = zlib.decompressobj(16 + zlib.MAX_WBITS)
                total += len(z.decompress(rest))
    if not z.eof or total < expected:
        return False
    cache[key] = True
    return True


# ----------------------------------------------------------------------------------------------- checks

def image_problems(f):
    """Why an image cannot be used as it is: [(kind, detail)], kind = "copy", "stretched" or "one slice"."""
    dims, spacing = image_header(f)
    meta = sidecar_of(f)[1]
    parts, size = f.name.split(".")[0].split("_"), "x".join(map(str, dims))
    problems = []
    if parts[-1] not in SUFFIXES or any(CONVERTER_COPY.search(p.split("-", 1)[-1]) for p in parts[1:-1]):
        problems.append(("copy", size))
    between = meta.get("SpacingBetweenSlices") or meta.get("SliceThickness") if isinstance(meta, dict) else None
    if len(dims) >= 3 and dims[2] > 1 and isinstance(between, (int, float)) and between > 0:
        middle = sorted(spacing[:3])[1]
        slices = max(spacing[:3], key=lambda s: abs(s - middle))  # the axis unlike the other two: defaced images are reoriented
        if slices > 1.25 * between:
            problems.append(("stretched", f"{slices:.2f} mm between slices; the scanner says {between:g} mm"))
    if len(dims) < 3 or dims[2] < 2:
        problems.append(("one slice", size))
    return problems


PROBLEM = {       # the defacing step repairs these itself (braims_repair.py): what is still reported was not repaired
    "copy": ("ERROR", "extra copy made by the converter, under a name that is not valid: run the defacing step, which "
                      "gives every copy a valid name"),
    "stretched": ("WARNING", "slice spacing does not match the scanner: slices are probably missing from the export, so "
                             "the image is stretched. Export the exam again if you can; tell the core center if you cannot"),
    "one slice": ("WARNING", "one slice only: if this is a scanner mosaic that could not be unpacked, export the series "
                             "again with all DICOM tags kept; tell the core center if you cannot"),
}


def all_files(bids):
    """Every file that could leave (everything outside code/, derivatives/ and sourcedata/)."""
    out = []
    for f in sorted(bids.rglob("*")):
        rel = f.relative_to(bids)
        if rel.parts[0] in LEAVE_OUT or not f.is_file():
            continue
        out.append(f)
    return out


def images(bids):
    return [f for f in all_files(bids) if f.name.endswith((".nii.gz", ".nii"))]


def check_files(bids, fix):
    """Only the kinds of files BRAIMS makes may leave; DICOM never."""
    for folder in LEAVE_OUT:
        if (bids / folder).exists():
            print(f"NOTE    {folder}/ stays at the site (the upload package leaves it out)")
    for sub in sorted(p for p in bids.glob("sub-*") if p.is_dir()):
        if not LOCAL_CODE.match(sub.name[4:]):
            report("ERROR", sub.name, "the code must be country + site + number, for example sub-BR040012")
    for f in all_files(bids):
        rel = f.relative_to(bids)
        if JUNK.match(f.name):
            if fix:
                f.unlink()
                report("FIXED", rel, "removed a file the operating system made")
            else:
                report("ERROR", rel, "file made by the operating system (run with --fix to remove it)")
            continue
        if is_dicom(f) or f.suffix.lower() in (".dcm", ".ima") or f.name.upper() == "DICOMDIR":
            report("ERROR", rel, "DICOM file; DICOM never leaves the site: move it out of the bids folder")
        elif not ALLOWED_FILES.match(rel.as_posix()):
            report("ERROR", rel, "this file may not leave the site (only BIDS images, sidecars and tables do): move it "
                                 "out of the bids folder")


def check_images(bids, cache):
    for f in images(bids):
        rel = f.relative_to(bids)
        if not f.name.endswith(".nii.gz"):
            continue                                           # reported by check_files
        try:
            if not image_complete(f, cache):
                report("ERROR", rel, "image file is incomplete or damaged: convert the session again")
                continue
            problems = image_problems(f)
        except Exception as e:                                 # noqa: BLE001 - one bad file must not stop the check
            report("ERROR", f"{rel}  ({type(e).__name__}: {e})", "image file cannot be read: convert the session again")
            continue
        side, meta = sidecar_of(f)
        if not side.exists():
            report("ERROR", rel, "image without its sidecar (.json)")
        if held(f):
            report("HOLD", f"{rel}  ({meta.get('BRAIMSHold')})", "held at the site: not in the package")
            continue
        for kind, detail in problems:
            report(PROBLEM[kind][0], f"{rel}  ({detail})", PROBLEM[kind][1])
        if f.name.endswith("_dwi.nii.gz"):
            stem = f.name[:-7]
            if not ((f.parent / (stem + ".bval")).exists() and (f.parent / (stem + ".bvec")).exists()):
                report("ERROR", rel, "diffusion image without its .bval/.bvec files")
        if meta.get("Defaced") is not True:
            report("ERROR", rel, "not defaced: run the defacing step")
        elif meta.get("DefacingNote"):
            report("WARNING", rel, "defaced, but no placement of the face mask passed the checks: look at the session's "
                                   "check picture; correct the mask by hand if the face shows (Part D of the manual)")


def clean_value(key, value):
    """(keep, value, problem): whether a sidecar value may leave, possibly redacted."""
    if key in ALLOWED_KEYS:
        if isinstance(value, str) and key in DESCRIPTIVE_KEYS:
            clean = mask_words(LONG_NUMBER.sub("#", DATE.sub("#", value)))
            if clean != value:
                return True, clean, "words that are not MRI terms, dates or long numbers in free text"
        if isinstance(value, (dict,)):
            return False, None, "nested values"
        return True, value, None
    if isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        ok = NUMBER_OK.match(key) and not NUMBER_NOT_OK.search(key)
        return (True, value, None) if ok else (False, None, "not an acquisition field")
    if isinstance(value, list) and value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        ok = NUMBER_OK.match(key) and not NUMBER_NOT_OK.search(key)
        return (True, value, None) if ok else (False, None, "not an acquisition field")
    return False, None, "not an acquisition field"


def check_json(bids, fix):
    for f in all_files(bids):
        if f.suffix != ".json":
            continue
        rel = f.relative_to(bids)
        try:
            data = read_json(f)
        except (ValueError, OSError) as e:
            report("ERROR", f"{rel}  ({e})", "sidecar cannot be read (not valid JSON): convert the session again")
            continue
        if not isinstance(data, dict):
            continue
        if f.name in ("dataset_description.json", "participants.json") or f.name.endswith("_scans.json"):
            removed = []
            if f.name == "participants.json":
                removed = sorted(k for k in data if not KEEP_COLUMN.match(k))
                for k in removed:
                    del data[k]
            if removed:
                if fix:
                    f.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
                    report("FIXED", rel, "removed " + ", ".join(removed))
                else:
                    report("ERROR", rel, "contains " + ", ".join(removed))
            continue
        out, removed, redacted = {}, [], []
        for k, v in data.items():
            keep, value, problem = clean_value(k, v)
            if not keep:
                removed.append(k)
            else:
                out[k] = value
                if value != v:
                    redacted.append(k)
        if removed or redacted:
            what = "; ".join(filter(None, [("removed " + ", ".join(removed)) if removed else "",
                                           ("redacted " + ", ".join(redacted)) if redacted else ""]))
            if fix:
                f.write_text(json.dumps(out, indent=4, ensure_ascii=False), encoding="utf-8")
                report("FIXED", rel, what)
            else:
                report("ERROR", rel, f"must change before upload: {what}")


def read_table(f):
    text, clean = read_text(f)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    head = lines[0] if lines else ""
    if "\t" not in head and (";" in head or "," in head):
        raise ValueError("not tab-separated (saved by Excel as ; or , separated): save it again as tab-separated text")
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter="\t"))
    return [r for r in rows if r], clean


def check_tsv(bids, fix):
    for f in all_files(bids):
        if f.suffix != ".tsv":
            continue
        rel = f.relative_to(bids)
        try:
            rows, clean = read_table(f)
        except (ValueError, OSError) as e:
            report("ERROR", f"{rel}  ({e})", "table cannot be read")
            continue
        if not rows:
            continue
        head, changed = rows[0], []
        rule = (KEEP_COLUMN if f.name == "participants.tsv" else SESSION_COLUMN if f.name.endswith("_sessions.tsv")
                else SCANS_COLUMN if f.name.endswith("_scans.tsv") else None)
        first = "participant_id" if f.name == "participants.tsv" else "session_id" if rule is SESSION_COLUMN else "filename"
        if rule is not None and (not head or head[0] != first):
            report("ERROR", rel, f"the first column must be {first}")
            continue
        if rule is not None:
            drop = [i for i, c in enumerate(head) if not rule.match(c)]
            if drop and fix:
                changed.append("removed columns " + ", ".join(head[i] for i in drop))
                rows = [[c for i, c in enumerate(r) if i not in drop] for r in rows]
                head = rows[0]
            elif drop:
                report("ERROR", rel, "contains the columns " + ", ".join(head[i] for i in drop))
        for i, c in enumerate(head):
            if c.startswith("age"):
                for r in rows[1:]:
                    if i < len(r) and r[i].replace(".", "", 1).isdigit() and float(r[i]) > 89:
                        if fix:
                            r[i] = "89"
                            changed.append(f"{r[0]}: age capped at 89")
                        else:
                            report("ERROR", f"{rel}  {r[0]}", "age above 89 (must be written as 89)")
        if f.name.endswith("_scans.tsv") and "acq_time" in head:
            i = head.index("acq_time")
            for r in rows[1:]:
                if i < len(r) and r[i] not in ("", "n/a") and not r[i].startswith(ANON_DATE):
                    if fix:
                        r[i] = ANON_DATE + r[i][10:] if len(r[i]) >= 10 else "n/a"
                        changed.append(f"{r[0]}: scan date replaced")
                    else:
                        report("ERROR", f"{rel}  {r[0]}", "real scan date in acq_time")
        for r in rows[1:]:
            for j, cell in enumerate(r):
                if DATE.search(cell) and not cell.startswith(ANON_DATE):
                    if fix and j > 0:
                        r[j] = "n/a"
                        changed.append(f"{r[0]}: date removed")
                    else:
                        report("ERROR", f"{rel}  {cell!r}", "a value is a date")
        if changed or (fix and not clean):
            with f.open("w", newline="", encoding="utf-8") as h:
                csv.writer(h, delimiter="\t", lineterminator="\n").writerows(rows)
            report("FIXED", f"{rel}  ({'; '.join(sorted(set(changed))) or 'saved as plain UTF-8'})", "table corrected")


def check_nifti(bids, fix):
    # NIfTI-1 header text fields: db_name (18 bytes at 14), descrip (80 at 148), aux_file (24 at 228)
    for f in images(bids):
        if not f.name.endswith(".nii.gz"):
            continue
        rel = f.relative_to(bids)
        try:
            with gzip.open(f, "rb") as h:
                head = h.read(352)
        except (OSError, EOFError, zlib.error):
            continue                                            # reported by check_images
        if len(head) < 348 or struct.unpack("<i", head[:4])[0] != 348:
            continue
        text = lambda a, n: head[a:a + n].split(b"\0")[0].decode("latin-1").strip()
        left = {name: text(a, n) for name, a, n in (("db_name", 14, 18), ("descrip", 148, 80), ("aux_file", 228, 24))
                if text(a, n)}
        if left and fix:
            with gzip.open(f, "rb") as h:
                data = bytearray(h.read())
            data[14:32], data[148:228], data[228:252] = bytes(18), bytes(80), bytes(24)
            with gzip.open(f, "wb") as h:
                h.write(data)
            report("FIXED", f"{rel}  {list(left.values())}", "cleared free text in the image header")
        elif left:
            report("ERROR", f"{rel}  {list(left.values())}", "free text in the image header")


# ----------------------------------------------------------------------------------------------- package

def package_items(bids):
    """Files that go into the package: allowed files, without held images and their companions."""
    hold = {f.name.split(".")[0] for f in images(bids) if held(f)}
    return [f for f in all_files(bids) if ALLOWED_FILES.match(f.relative_to(bids).as_posix())
            and f.name.split(".")[0] not in hold]


def filtered_table(f, bids, images_in):
    """A table as it goes into the package: only rows about images, sessions and people that are in the package.
    None when no row is left."""
    rows, _ = read_table(f)
    if not rows:
        return None
    rel = f.relative_to(bids)
    if f.name.endswith("_scans.tsv"):
        ses = rel.parent.as_posix()
        keep = [r for r in rows[1:] if f"{ses}/{r[0]}" in images_in]
    elif f.name.endswith("_sessions.tsv"):
        keep = [r for r in rows[1:] if any(i.startswith(f"{rel.parent.as_posix()}/{r[0]}/") for i in images_in)]
    elif f.name == "participants.tsv":
        keep = [r for r in rows[1:] if any(i.startswith(f"{r[0]}/") for i in images_in)]
    else:
        keep = rows[1:]
    if not keep:
        return None
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerows([rows[0]] + keep)
    return buf.getvalue().encode("utf-8")


def package(bids, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    items = package_items(bids)
    images_in = {f.relative_to(bids).as_posix() for f in items if f.name.endswith(".nii.gz")}
    lines = ["file\tbytes\tsha256"]
    tmp = target.with_suffix(".partial")
    count = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for f in items:
            rel = f.relative_to(bids).as_posix()
            if f.suffix == ".tsv":
                data = filtered_table(f, bids, images_in)
                if data is None:
                    continue
                z.writestr(rel, data)
                lines.append(f"{rel}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}")
            else:
                digest = hashlib.sha256()
                with open(f, "rb") as h:
                    while chunk := h.read(1 << 20):
                        digest.update(chunk)
                z.write(f, rel)
                lines.append(f"{rel}\t{f.stat().st_size}\t{digest.hexdigest()}")
            count += 1
        z.writestr("manifest.tsv", "\n".join(lines) + "\n")
    tmp.replace(target)
    digest = hashlib.sha256()
    with open(target, "rb") as h:
        while chunk := h.read(1 << 20):
            digest.update(chunk)
    Path(str(target) + ".sha256").write_text(f"{digest.hexdigest()}  {target.name}\n")
    print(f"\nPackage: {target}  ({count} files, {target.stat().st_size / 1e6:.1f} MB)")
    print(f"SHA-256: {digest.hexdigest()}")


def load_cache(bids):
    try:
        return json.loads((bids / "code" / "braims" / "verified.json").read_text())
    except (OSError, ValueError):
        return {}


def save_cache(bids, cache):
    try:
        p = bids / "code" / "braims" / "verified.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache))
    except OSError:
        pass


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
    print(f"BRAIMS check {VERSION} of {bids}\n")
    cache = load_cache(bids)
    for step in (lambda: check_files(bids, fix), lambda: check_json(bids, fix), lambda: check_tsv(bids, fix),
                 lambda: check_nifti(bids, fix), lambda: check_images(bids, cache)):
        try:
            step()
        except Exception as e:                                 # noqa: BLE001 - the check reports, it never crashes
            report("ERROR", f"{type(e).__name__}: {e}", "the check itself failed here: tell the core center")
    save_cache(bids, cache)
    show("FIXED")
    errors, warnings = show("ERROR"), show("WARNING")
    holds = show("HOLD")
    print(f"\n{errors} error(s), {warnings} warning(s), {holds} image(s) held at the site.")
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
