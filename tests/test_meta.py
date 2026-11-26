"""Pre-flight checks, scanner IDs, days between sessions and safe re-runs."""
import json, sys
from pathlib import Path
import numpy as np
import nibabel as nib

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "braims"), str(ROOT / "tests")]
import braims_meta as meta                                      # noqa: E402
from synth_dicom import write_series                            # noqa: E402


def head(tmp_path):
    p = tmp_path / "head.nii.gz"
    nib.Nifti1Image(np.random.rand(40, 40, 40).astype(np.float32) * 1000, np.diag([4, 4, 4, 1])).to_filename(p)
    return p


def exam(raw, sub, ses, h, date="20190614", patient=None, n=1):
    write_series(h, raw / sub / ses / f"s{n}", "t1", "ax", 8, fov=(160, 160, 32), study={"date": date}, patient=patient,
                 series_number=n)


def test_preflight_finds_bad_names_and_mixed_patients(tmp_path):
    raw, h = tmp_path / "raw", head(tmp_path)
    exam(raw, "sub-BR010001", "ses-01", h)
    exam(raw, "sub-BR010002", "ses-01", h)
    exam(raw, "sub-BR010002", "ses-01", h, patient={"PatientID": "OTHER", "PatientName": "OUTRO"}, n=2)
    exam(raw, "sub-br010003", "ses-01", h)
    exam(raw, "sub-BR010004", "session1", h)
    (raw / "stray.dcm").write_bytes(b"x")
    found = dict(meta.preflight(raw, tmp_path))
    assert "sub-br010003" in found and "sub-BR010004/session1" in found and "stray.dcm" in found
    assert "different patients" in found["sub-BR010002/ses-01"]
    assert not any(k.startswith("sub-BR010001") for k in found)


def test_days_from_baseline(tmp_path):
    raw, bids, h = tmp_path / "raw", tmp_path / "bids", head(tmp_path)
    for ses, date in (("ses-01", "20190614"), ("ses-02", "20200901"), ("ses-03", "")):
        exam(raw, "sub-BR010001", ses, h, date=date or "20190614")
        (bids / "sub-BR010001" / ses / "anat").mkdir(parents=True)
    meta.write_sessions(bids, raw)
    text = (bids / "sub-BR010001" / "sub-BR010001_sessions.tsv").read_text()
    assert text.splitlines() == ["session_id\tdays_from_baseline", "ses-01\t0", "ses-02\t445", "ses-03\t0"]
    assert "2019" not in text


def test_scanner_ids_are_stable_and_distinct(tmp_path):
    bids = tmp_path / "bids"
    a = bids / "sub-BR010001" / "ses-01" / "anat"
    a.mkdir(parents=True)
    for name, serial in (("a", "111"), ("b", "111"), ("c", "222")):
        (a / f"{name}.json").write_text(json.dumps({"DeviceSerialNumber": serial, "StationName": "MR1"}))
    meta.scanner_ids(bids)
    ids = {n: json.loads((a / f"{n}.json").read_text())["BRAIMSScannerID"] for n in "abc"}
    assert ids["a"] == ids["b"] != ids["c"] and "111" not in ids["a"]
    meta.scanner_ids(bids)                                      # idempotent
    assert json.loads((a / "a.json").read_text())["BRAIMSScannerID"] == ids["a"]


def test_rerun_safety(tmp_path):
    raw, bids, h = tmp_path / "raw", tmp_path / "bids", head(tmp_path)
    exam(raw, "sub-BR010001", "ses-01", h)
    (bids / "sub-BR010001" / "ses-01" / "anat").mkdir(parents=True)
    assert meta.stale_sessions(raw, bids) == []                 # upgrade: existing sessions adopted, nothing deleted
    assert meta.stale_sessions(raw, bids) == []
    exam(raw, "sub-BR010001", "ses-01", h, n=2)                  # a series added to the exam later
    assert [s.name for s in meta.stale_sessions(raw, bids)] == ["ses-01"]
    meta.mark_converted(raw, bids)
    assert meta.stale_sessions(raw, bids) == []
    (bids / "sub-BR010001" / "ses-02").mkdir()                   # converted without a marker (interrupted run)
    exam(raw, "sub-BR010001", "ses-02", h)
    assert [s.name for s in meta.stale_sessions(raw, bids)] == ["ses-02"]
