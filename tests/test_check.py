"""The check lets out only what may leave, never crashes, and packages only clean, complete data."""
import gzip, json, subprocess, sys, zipfile
from pathlib import Path
import numpy as np
import nibabel as nib
import pytest

CHECK = Path(__file__).resolve().parents[1] / "braims" / "braims_check.py"
SUB, SES = "sub-BR010001", "ses-01"


def run(bids, *args):
    out = subprocess.run([sys.executable, str(CHECK), str(bids), *args], capture_output=True, text=True)
    assert "Traceback" not in out.stderr, out.stderr
    return out.returncode, out.stdout


def image(path, meta=None, descrip=b"", db=b"", aux=b"", defaced=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = nib.Nifti1Image((np.random.rand(20, 20, 20) * 1000).astype(np.int16), np.eye(4))
    im.header["descrip"], im.header["db_name"], im.header["aux_file"] = descrip, db, aux
    im.to_filename(path)
    m = {"Defaced": defaced, "Manufacturer": "Siemens", "SliceThickness": 1, "EchoTime": 0.003}
    m.update(meta or {})
    path.with_name(path.name[:-7] + ".json").write_text(json.dumps(m))
    return path


@pytest.fixture
def bids(tmp_path):
    b = tmp_path / "bids"
    a = b / SUB / SES / "anat"
    image(a / f"{SUB}_{SES}_acq-MPRAGE_T1w.nii.gz", descrip=b"TE=3;2019-06-14", db=b"JOAO", aux=b"HOSP", meta={
        "PatientName": "SILVA^JOAO", "InstitutionName": "Hospital X", "ProtocolName": "Joao Silva cranio 1234567",
        "SeriesDescription": "t1 2019-06-14", "global": {"const": {"PatientID": "1234567", "PatientBirthDate": "19700101"}},
        "ReferringPhysicianName": "DR HOUSE", "AcquisitionDateTime": "2019-06-14T10:10:10",
        "PerformedProcedureStepStartDate": "20190614", "OtherPatientIDs": "998877", "PatientAddress": "Rua B",
        "DeviceSerialNumber": "12345", "StationName": "MRC1", "PatientWeight": 70, "PatientSize": 1.7,
        "RepetitionTime": 2.3, "MultibandAccelerationFactor": 2, "SomeNewVendorNumber": 4.5})
    image(a / f"{SUB}_{SES}_acq-2D_T2w.nii.gz")
    (b / "participants.tsv").write_text("participant_id\tname\tage\tsex\nsub-BR010001\tJoao\t92\tM\n")
    (b / SUB / SES / f"{SUB}_{SES}_scans.tsv").write_text(
        f"filename\tacq_time\nanat/{SUB}_{SES}_acq-MPRAGE_T1w.nii.gz\t2019-06-14T10:10:10\n")
    (b / SUB / f"{SUB}_sessions.tsv").write_text("session_id\tacq_date\tdays_from_baseline\nses-01\t14/06/2019\t0\n")
    (b / "dataset_description.json").write_text(json.dumps({"Name": "BRAIMS", "BIDSVersion": "1.9.0"}))
    (b / "Thumbs.db").write_bytes(b"x")
    (b / ".DS_Store").write_bytes(b"x")
    x = b / "dicom_backup"
    x.mkdir()
    (x / "IM0001").write_bytes(b"\0" * 128 + b"DICM" + b"PN SILVA^JOAO")
    (b / "site_key_copy.xlsx").write_bytes(b"PK names")
    (b / "screenshot.png").write_bytes(b"\x89PNG")
    (a / "notes.txt").write_text("Joao Silva 1234567")
    return b


def test_planted_leaks_never_reach_the_package(bids, tmp_path):
    code, out = run(bids, "--fix")
    for stray in ("dicom_backup/IM0001", "site_key_copy.xlsx", "screenshot.png", "notes.txt"):
        assert stray in out
    assert code == 1                                            # stray files are errors until moved out
    for stray in ("dicom_backup", "site_key_copy.xlsx", "screenshot.png"):
        p = bids / stray
        __import__("shutil").rmtree(p) if p.is_dir() else p.unlink()
    (bids / SUB / SES / "anat" / "notes.txt").unlink()
    code, out = run(bids, "--package", str(tmp_path / "out" / "p.zip"))
    assert code == 0, out
    with zipfile.ZipFile(tmp_path / "out" / "p.zip") as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert not any(n.endswith(("Thumbs.db", ".DS_Store")) for n in names)
    for secret in (b"SILVA", b"Joao", b"JOAO", b"1234567", b"19700101", b"998877", b"Rua B", b"HOUSE", b"Hospital X",
                   b"12345", b"MRC1", b"2019-06-14", b"20190614", b"14/06/2019", b"HOSP"):
        assert secret not in blob, secret
    side = json.loads(zipfile.ZipFile(tmp_path / "out" / "p.zip").read(f"{SUB}/{SES}/anat/{SUB}_{SES}_acq-MPRAGE_T1w.json"))
    assert side["RepetitionTime"] == 2.3 and side["MultibandAccelerationFactor"] == 2 and side["SomeNewVendorNumber"] == 4.5
    assert "PatientWeight" not in side and "PatientSize" not in side


def test_excel_bom_keeps_participant_id(bids):
    (bids / "participants.tsv").write_bytes("\ufeffparticipant_id\tage\tsex\r\nsub-BR010001\t45\tF\r\n".encode("utf-8"))
    run(bids, "--fix")
    text = (bids / "participants.tsv").read_text(encoding="utf-8")
    assert text.startswith("participant_id\tage\tsex\n")


def test_latin1_table_is_read_and_rewritten(bids):
    (bids / "participants.tsv").write_bytes("participant_id\tage\tsex\tobservação\nsub-BR010001\t45\tF\tJoão\n".encode("latin-1"))
    run(bids, "--fix")
    text = (bids / "participants.tsv").read_text(encoding="utf-8")
    assert "João" not in text and text.startswith("participant_id\tage\tsex")


def test_semicolon_table_is_an_error(bids):
    (bids / "participants.tsv").write_text("participant_id;age;sex\nsub-BR010001;45;F\n")
    code, out = run(bids, "--fix")
    assert code == 1 and "tab-separated" in out


def test_bad_json_is_an_error_not_a_crash(bids):
    (bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-2D_T2w.json").write_text('{"Defaced": true,}')
    code, out = run(bids)
    assert code == 1 and "not valid JSON" in out


def test_truncated_image_is_an_error(bids):
    p = bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-2D_T2w.nii.gz"
    data = p.read_bytes()
    p.write_bytes(data[:len(data) // 2])
    code, out = run(bids)
    assert code == 1 and "incomplete or damaged" in out


def test_stray_nii_like_file_is_an_error_not_a_crash(bids):
    (bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-2D_T2w.nii.gz.bak").write_bytes(b"x")
    code, out = run(bids)
    assert code == 1 and ".bak" in out


def test_undefaced_image_blocks_the_package(bids, tmp_path):
    image(bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-3D_FLAIR.nii.gz", defaced=False)
    code, out = run(bids, "--fix", "--package", str(tmp_path / "p.zip"))
    assert code == 1 and "not defaced" in out and not (tmp_path / "p.zip").exists()


def test_held_images_stay_at_the_site(bids, tmp_path):
    for stray in ("dicom_backup", "site_key_copy.xlsx", "screenshot.png"):
        p = bids / stray
        __import__("shutil").rmtree(p) if p.is_dir() else p.unlink()
    (bids / SUB / SES / "anat" / "notes.txt").unlink()
    image(bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-cspineSagT2_T2w.nii.gz", defaced=False,
          meta={"BRAIMSHold": "spine: kept at the site until spine defacing is available"})
    run(bids, "--fix")
    code, out = run(bids, "--package", str(tmp_path / "p.zip"))
    assert code == 0, out
    assert "held at the site" in out
    names = zipfile.ZipFile(tmp_path / "p.zip").namelist()
    assert not any("cspine" in n for n in names)


def test_dwi_without_bval_is_an_error(bids):
    image(bids / SUB / SES / "dwi" / f"{SUB}_{SES}_acq-DTI_dwi.nii.gz")
    code, out = run(bids)
    assert ".bval" in out and code == 1


def test_tables_list_only_what_is_packaged(bids, tmp_path):
    for stray in ("dicom_backup", "site_key_copy.xlsx", "screenshot.png"):
        p = bids / stray
        __import__("shutil").rmtree(p) if p.is_dir() else p.unlink()
    (bids / SUB / SES / "anat" / "notes.txt").unlink()
    held = "sub-BR010005"
    image(bids / held / SES / "anat" / f"{held}_{SES}_acq-orbitSTIR_T2w.nii.gz", meta={"BRAIMSHold": "no head image"})
    (bids / held / SES / f"{held}_{SES}_scans.tsv").write_text(f"filename\tacq_time\nanat/{held}_{SES}_acq-orbitSTIR_T2w.nii.gz\tn/a\n")
    (bids / held / f"{held}_sessions.tsv").write_text("session_id\tdays_from_baseline\nses-01\t0\n")
    with open(bids / "participants.tsv", "a") as h:
        h.write(f"{held}\t40\tF\n")
    run(bids, "--fix")
    code, out = run(bids, "--package", str(tmp_path / "p.zip"))
    assert code == 0, out
    z = zipfile.ZipFile(tmp_path / "p.zip")
    assert not any(held in n for n in z.namelist())
    assert held.encode() not in z.read("participants.tsv")
    assert b"sub-BR010001" in z.read("participants.tsv")


def test_spine_without_defacing_is_packaged(bids, tmp_path):
    for stray in ("dicom_backup", "site_key_copy.xlsx", "screenshot.png"):
        p = bids / stray
        __import__("shutil").rmtree(p) if p.is_dir() else p.unlink()
    (bids / SUB / SES / "anat" / "notes.txt").unlink()
    image(bids / SUB / SES / "anat" / f"{SUB}_{SES}_acq-cspineSagT2_T2w.nii.gz", defaced=False,
          meta={"BRAIMSRegion": "spine", "DefacingMethod": "not needed: spine image"})
    run(bids, "--fix")
    code, out = run(bids, "--package", str(tmp_path / "p.zip"))
    assert code == 0, out
    assert any("cspine" in n for n in zipfile.ZipFile(tmp_path / "p.zip").namelist())
