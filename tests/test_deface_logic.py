"""Defacing logic that does not need FSL: orbit cones, reference choice, holds, masks applied from the kept original."""
import json, os, sys, time
from pathlib import Path
import numpy as np
import nibabel as nib
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def D(tmp_path_factory):
    stub = tmp_path_factory.mktemp("stub")
    (stub / "pydeface" / "data").mkdir(parents=True)
    (stub / "pydeface" / "__init__.py").write_text("")
    a = np.eye(4)
    a[:3, 3] = (-96, -132, -78)
    nib.Nifti1Image(np.ones((193, 229, 193), np.uint8), a).to_filename(stub / "pydeface" / "data" / "facemask.nii.gz")
    sys.path[:0] = [str(stub), str(ROOT / "braims")]
    argv = sys.argv
    sys.argv = ["braims_deface.py", str(tmp_path_factory.mktemp("bids"))]
    try:
        import braims_deface
    finally:
        sys.argv = argv
    return braims_deface


def setup_bids(D, root):
    D.bids = root
    D.saved = root / "derivatives" / "braims_deface"
    D.reapply = False


def image(p, shape=(40, 40, 30), mr="3D", meta=None, value=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.rand(*shape).astype(np.float32) * 100 + 1 if value is None else np.full(shape, value, np.float32)
    nib.Nifti1Image(data, np.eye(4)).to_filename(p)
    p.with_name(p.name[:-7] + ".json").write_text(json.dumps({"MRAcquisitionType": mr, **(meta or {})}))
    return p


def test_cone_stops_3mm_in_front_of_the_globe(D):
    g, ap = D.EYES[0], D.APEXES[0]
    aff = np.eye(4)
    aff[:3, 3] = g - 45
    keep = D.orbit_cones((91, 91, 91), aff, D.EYES[:1], D.APEXES[:1])
    xyz = nib.affines.apply_affine(aff, np.argwhere(keep)) - g
    front = (g - ap) / np.linalg.norm(g - ap)
    assert np.max(xyz @ front) <= D.GLOBE_FRONT + 1.0           # eyelids (12+ mm in front of the cornea) removed
    back = (ap - g) / np.linalg.norm(ap - g)
    assert np.max(xyz @ back) >= np.linalg.norm(ap - g) - 1      # still reaches the orbital apex
    side = xyz - np.outer(xyz @ front, front)
    assert np.max(np.linalg.norm(side, axis=1)) >= D.ORBIT_RADIUS[0] - 1   # 12 mm around the globe kept


def test_unsure_eyes_keep_a_wider_margin_in_front(D):
    g, ap = D.EYES[0], D.APEXES[0]
    aff = np.eye(4)
    aff[:3, 3] = g - 50
    keep = D.orbit_cones((101, 101, 101), aff, D.EYES[:1], D.APEXES[:1], D.ORBIT_UNSURE)
    xyz = nib.affines.apply_affine(aff, np.argwhere(keep)) - g
    front = (g - ap) / np.linalg.norm(g - ap)
    assert D.GLOBE_FRONT < np.max(xyz @ front) <= D.GLOBE_FRONT + D.ORBIT_UNSURE + 1


def test_reference_skips_spine_orbit_and_held(D, tmp_path):
    setup_bids(D, tmp_path)
    a = tmp_path / "sub-BR010001" / "ses-01" / "anat"
    brain = image(a / "sub-BR010001_ses-01_acq-2D_T2w.nii.gz", (40, 40, 24), "2D")
    spine = image(a / "sub-BR010001_ses-01_acq-cspineAxT2_T2w.nii.gz", (40, 40, 60), "2D")
    orbit = image(a / "sub-BR010001_ses-01_acq-orbitSTIR_T2w.nii.gz", (40, 40, 50), "2D")
    held = image(a / "sub-BR010001_ses-01_acq-3D_T1w.nii.gz", (40, 40, 80), "3D", {"BRAIMSHold": "possible spine"})
    assert D.reference([brain, spine, orbit, held]) == brain
    assert D.reference([spine, orbit, held]) is None


def test_apply_keep_starts_from_the_original_each_time(D, tmp_path):
    setup_bids(D, tmp_path)
    img = image(tmp_path / "sub-BR010001" / "ses-01" / "anat" / "sub-BR010001_ses-01_acq-3D_T1w.nii.gz", value=7.0)
    small, big = np.zeros((40, 40, 30), np.uint8), np.zeros((40, 40, 30), np.uint8)
    small[10:20], big[5:35] = 1, 1
    for name, m in (("small", small), ("big", big)):
        nib.Nifti1Image(m, np.eye(4)).to_filename(tmp_path / f"{name}.nii.gz")
    D.apply_keep(img, tmp_path / "small.nii.gz")                 # removes too much
    assert np.asanyarray(nib.load(img).dataobj)[5:35].min() == 0
    D.apply_keep(img, tmp_path / "big.nii.gz")                   # corrected mask: tissue comes back
    d = np.asanyarray(nib.load(img).dataobj)
    assert d[5:35].min() == 7.0 and d[:5].max() == 0
    assert D.original_of(img).exists() and np.asanyarray(nib.load(D.original_of(img)).dataobj).min() == 7.0
    assert not list(img.parent.glob("*.partial*"))


def test_reapply_picks_up_edited_masks_on_defaced_images(D, tmp_path):
    setup_bids(D, tmp_path)
    ses = tmp_path / "sub-BR010001" / "ses-01"
    img = image(ses / "anat" / "sub-BR010001_ses-01_acq-3D_T1w.nii.gz", value=7.0)
    m = np.zeros((40, 40, 30), np.uint8)
    m[10:20] = 1
    nib.Nifti1Image(m, np.eye(4)).to_filename(tmp_path / "m.nii.gz")
    D.apply_keep(img, tmp_path / "m.nii.gz")
    side = img.with_name(img.name[:-7] + ".json")
    side.write_text(json.dumps({"Defaced": True}))
    assert D.reapply_masks(ses, [img]) == 0                      # nothing edited: nothing done
    time.sleep(1.1)
    m[:] = 1
    nib.Nifti1Image(m, np.eye(4)).to_filename(D.saved_mask(img))  # edited in ITK-SNAP: keep everything
    assert D.reapply_masks(ses, [img]) == 1
    assert np.asanyarray(nib.load(img).dataobj).min() == 7.0


def test_diffusion_mask_gets_a_margin(D, tmp_path):
    setup_bids(D, tmp_path)
    img = image(tmp_path / "sub-BR010001" / "ses-01" / "dwi" / "sub-BR010001_ses-01_acq-DTI_dwi.nii.gz", (40, 40, 30, 3), value=5.0)
    m = np.zeros((40, 40, 30), np.uint8)
    m[10:30, 10:30, 10:20] = 1
    nib.Nifti1Image(m, np.eye(4)).to_filename(tmp_path / "m.nii.gz")
    D.apply_keep(img, tmp_path / "m.nii.gz", margin=5.0)
    d = np.asanyarray(nib.load(img).dataobj)
    assert d.shape == (40, 40, 30, 3) and d[7, 20, 15, 2] == 5.0 and d[2, 20, 15, 0] == 0


def test_orbit_only_session_is_held(D, tmp_path):
    setup_bids(D, tmp_path)
    ses = tmp_path / "sub-BR010005" / "ses-01"
    image(ses / "anat" / "sub-BR010005_ses-01_acq-orbitSTIR_T2w.nii.gz", (40, 40, 16), "2D")
    image(ses / "anat" / "sub-BR010005_ses-01_acq-cspineSagT2_T2w.nii.gz", (40, 40, 16), "2D")
    text, done, failed, look = D.deface_session(ses)
    metas = [json.loads(p.read_text()) for p in sorted((ses / "anat").glob("*.json"))]
    assert all(m.get("BRAIMSHold") for m in metas) and not any(m.get("Defaced") for m in metas)
    assert done == 0 and not failed


def test_workers_respect_memory(D):
    assert 1 <= D.workers_for(100) <= (os.cpu_count() or 1)
    assert D.workers_for(1) == 1
