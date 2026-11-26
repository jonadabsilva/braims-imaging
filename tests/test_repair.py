"""Repairs before defacing: converter copies, stretched images, broken headers, scanner mosaics."""
import json, sys
from pathlib import Path
import numpy as np
import nibabel as nib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "braims"))
import braims_repair as R                                       # noqa: E402


def mk(p, shape, aff=np.eye(4), meta=None):
    nib.Nifti1Image(np.random.rand(*shape).astype(np.float32) * 100, aff).to_filename(p)
    if meta is not None:
        p.with_name(p.name[:-7] + ".json").write_text(json.dumps(meta))


def test_own_name():
    assert R.own_name("sub-X_ses-01_acq-SE_T1wa") == "sub-X_ses-01_acq-SE_T1w"
    assert R.own_name("sub-X_ses-01_acq-SEreala_T1w") == "sub-X_ses-01_acq-SE_part-real_T1w"
    assert R.own_name("sub-X_ses-01_acq-2DTilt1_T2w") == "sub-X_ses-01_acq-2D_T2w"
    assert R.own_name("sub-X_ses-01_acq-SE_T1w") is None
    assert R.own_name("sub-X_ses-01_acq-SE_echo-2_PDT2") is None


def test_session_repairs(tmp_path):
    s = tmp_path / "sub-X" / "ses-01"
    a = s / "anat"
    a.mkdir(parents=True)
    mk(a / "sub-X_ses-01_acq-SE_T1w.nii.gz", (64, 64, 20), meta={})
    mk(a / "sub-X_ses-01_acq-SE_T1wa.nii.gz", (64, 64, 24), meta={})
    mk(a / "sub-X_ses-01_acq-2D_T2w.nii.gz", (64, 64, 20), meta={"SliceThickness": 5})
    mk(a / "sub-X_ses-01_acq-2DEq1_T2w.nii.gz", (64, 64, 30))
    im = nib.Nifti1Image(np.ones((10, 10, 10), np.float32), np.eye(4))
    im.header.set_zooms((1, 1, 5))
    im.set_qform(np.diag([1, 1, 5, 1]), 1)
    im.set_sform(np.eye(4), 1)
    im.to_filename(a / "sub-X_ses-01_acq-HDR_T2w.nii.gz")
    (a / "sub-X_ses-01_acq-HDR_T2w.json").write_text("{}")
    tiles = np.zeros((128, 128), np.float32)
    for k in range(13):
        r, c = divmod(k, 4)
        tiles[c * 32:(c + 1) * 32, (3 - r) * 32:(4 - r) * 32] = k + 1
    nib.Nifti1Image(tiles[:, :, None], np.eye(4)).to_filename(a / "sub-X_ses-01_acq-MOS_T2w.nii.gz")
    (a / "sub-X_ses-01_acq-MOS_T2w.json").write_text(json.dumps({"ImageType": ["ORIGINAL", "PRIMARY", "M", "MOSAIC"],
                                                                 "AcquisitionMatrixPE": 32}))
    (s / "sub-X_ses-01_scans.tsv").write_text("filename\tacq_time\n" + "".join(
        f"anat/{p.name}\t1925-01-01T10:00:00\n" for p in sorted(a.glob("*.nii.gz"))))
    R.repair(s)
    names = sorted(p.name for p in a.glob("*.nii.gz"))
    assert names == ["sub-X_ses-01_acq-2D_T2w.nii.gz", "sub-X_ses-01_acq-HDR_T2w.nii.gz", "sub-X_ses-01_acq-MOS_T2w.nii.gz",
                     "sub-X_ses-01_acq-SE_T1w.nii.gz", "sub-X_ses-01_acq-SE_rec-copy2_T1w.nii.gz"]
    assert nib.load(a / "sub-X_ses-01_acq-2D_T2w.nii.gz").shape == (64, 64, 30)
    assert nib.load(a / "sub-X_ses-01_acq-SE_T1w.nii.gz").shape == (64, 64, 24)
    d = np.asanyarray(nib.load(a / "sub-X_ses-01_acq-MOS_T2w.nii.gz").dataobj)
    assert [int(d[..., k].max()) for k in range(d.shape[2])] == list(range(1, 14))
    assert np.allclose(nib.load(a / "sub-X_ses-01_acq-HDR_T2w.nii.gz").header.get_sform(), np.diag([1, 1, 5, 1]))
    for p in a.glob("*.nii.gz"):
        assert json.loads(p.with_name(p.name[:-7] + ".json").read_text()).get("ImageRepair") or p.name.startswith("x")
    listed = [l.split("\t")[0] for l in (s / "sub-X_ses-01_scans.tsv").read_text().splitlines()[1:]]
    assert sorted(listed) == sorted(f"anat/{n}" for n in names)
