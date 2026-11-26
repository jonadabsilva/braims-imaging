"""Every series name seen at BRAIMS sites (Siemens, GE, Philips, Canon; Spanish, Portuguese, English) gets the right BIDS name."""
import sys
from pathlib import Path
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "braims"))
import braims                                                   # noqa: E402
import braims_naming as naming                                  # noqa: E402

# (series description, MRAcquisitionType, BIDScoin's own datatype, expected BIDS name, DICOM hint)
CASES = [
    ("t1_mprage_sag_p2_iso", "3D", "anat", "anat/acq-MPRAGE_T1w", {}),
    ("SAG FSPGR BRAVO", "3D", "anat", "anat/acq-3D_T1w", {}),
    ("3D FSPGR", "3D", "extra_data", "anat/acq-3D_T1w", {}),
    ("sT1W_3D_TFE", "3D", "anat", "anat/acq-3D_T1w", {}),
    ("T1W_3D_FFE", "3D", "anat", "anat/acq-3D_T1w", {}),
    ("3D T1 TFE pos gado", "3D", "anat", "anat/acq-3D_ce-gadolinium_T1w", {}),
    ("MPRAGE POS CONTRASTE", "3D", "anat", "anat/acq-MPRAGE_ce-gadolinium_T1w", {}),
    ("t1_mprage_sag_p2_iso_POS", "3D", "anat", "anat/acq-MPRAGE_ce-gadolinium_T1w", {}),
    ("T1 3D pós-contraste", "3D", "anat", "anat/acq-3D_ce-gadolinium_T1w", {}),
    ("T1 3D pre contraste", "3D", "anat", "anat/acq-3D_T1w", {}),
    ("T1 3D sin contraste", "3D", "anat", "anat/acq-3D_T1w", {}),
    ("t1_mprage_sag", "3D", "anat", "anat/acq-MPRAGE_ce-gadolinium_T1w", {"ContrastBolusAgent": "Gadovist"}),
    ("T1 SPACE post", "3D", "anat", "anat/acq-3D_ce-gadolinium_T1w", {}),
    ("mp2rage_UNI_Images", "3D", "extra_data", "anat/acq-MP2RAGE_UNIT1", {}),
    ("mp2rage_INV1", "3D", "extra_data", "anat/acq-MP2RAGEinv1_MP2RAGE", {}),
    ("Ax T1 SE", "2D", "anat", "anat/acq-SE_T1w", {}),
    ("SAG T1 FLAIR", "2D", "anat", "anat/acq-T1FLAIR_T1w", {}),
    ("AX T1 FLAIR +C", "2D", "anat", "anat/acq-T1FLAIR_ce-gadolinium_T1w", {}),
    ("t2_tirm_tra_dark-fluid", "2D", "anat", "anat/acq-2D_FLAIR", {}),
    ("t2_spc_da-fl_sag_p2_iso", "3D", "anat", "anat/acq-3D_FLAIR", {}),
    ("Sag CUBE FLAIR", "3D", "anat", "anat/acq-3D_FLAIR", {}),
    ("3D_Brain_VIEW_FLAIR_SHC", "3D", "anat", "anat/acq-3D_FLAIR", {}),
    ("FLAIR 3D pos contraste", "3D", "anat", "anat/acq-3D_ce-gadolinium_FLAIR", {}),
    ("AX T2 FLAIR", "2D", "anat", "anat/acq-2D_FLAIR", {}),
    ("t2_tse_tra", "2D", "anat", "anat/acq-2D_T2w", {}),
    ("CUBE T2", "3D", "anat", "anat/acq-3D_T2w", {}),
    ("Ax T2 GRE", "2D", "anat", "anat/acq-GRE_T2starw", {}),
    ("t2_fl2d_tra_hemo", "2D", "anat", "anat/acq-GRE_T2starw", {}),
    ("AX T2* GRE", "2D", "anat", "anat/acq-GRE_T2starw", {}),
    ("Ax PD", "2D", "extra_data", "anat/acq-2D_PDw", {}),
    ("pd+t2_tse_tra", "2D", "anat", "anat/acq-2D_PDT2", {}),
    ("DIR_SPACE", "3D", "anat", "anat/acq-DIR_FLAIR", {}),
    ("Mag_Images", "3D", "anat", "anat/acq-SWI_part-mag_T2starw", {}),
    ("Pha_Images", "3D", "anat", "anat/acq-SWI_part-phase_T2starw", {}),
    ("SWI_Images", "3D", "anat", "excluded", {}),
    ("mIP_Images(SW)", "3D", "anat", "excluded", {}),
    ("ep2d_diff_mddw_30", "2D", "dwi", "dwi/acq-ep2ddiffmddw30_dwi", {}),
    ("Ax DWI", "2D", "dwi", "dwi/acq-AxDWI_dwi", {}),
    ("Ax DWI ADC", "2D", "dwi", "excluded", {}),
    ("ep2d_diff_TRACEW", "2D", "dwi", "excluded", {}),
    ("Apparent Diffusion Coefficient (mm2/s)", "2D", "extra_data", "excluded", {}),
    ("DTI_32dir", "2D", "dwi", "dwi/acq-DTI32dir_dwi", {}),
    # orbits and optic nerves
    ("ÓRBITAS STIR COR", "2D", "anat", "anat/acq-orbitSTIR_T2w", {}),
    ("Órbita T1 FS pós", "2D", "anat", "anat/acq-orbitT1_ce-gadolinium_T1w", {}),
    ("nervos opticos t2 cor fs", "2D", "anat", "anat/acq-orbit_T2w", {}),
    ("COR STIR NERVIO OPTICO", "2D", "anat", "anat/acq-orbitSTIR_T2w", {}),
    ("t2_tse_cor_orbit_fs", "2D", "anat", "anat/acq-orbit_T2w", {}),
    ("cor t1 fs orbitas gado", "2D", "anat", "anat/acq-orbitT1_ce-gadolinium_T1w", {}),
    ("ax t1 orbita", "2D", "anat", "anat/acq-orbitT1_T1w", {}),
    ("Cor T2 FS NN OO", "2D", "anat", "anat/acq-orbit_T2w", {}),
    # spine: named by region, plane and contrast, held at the site
    ("SAG T2 COLUNA CERVICAL", "2D", "anat", "anat/acq-cspineSagT2_T2w", {}),
    ("sag stir medula", "2D", "extra_data", "anat/acq-spineSagSTIR_T2w", {}),
    ("t2_tse_sag_cspine", "2D", "anat", "anat/acq-cspineSagT2_T2w", {}),
    ("AX T2 MEDULA CERVICAL", "2D", "anat", "anat/acq-cspineAxT2_T2w", {}),
    ("STIR SAG COLUMNA DORSAL", "2D", "extra_data", "anat/acq-tspineSagSTIR_T2w", {}),
    ("Sag T1 post gado coluna toracica", "2D", "anat", "anat/acq-tspineSagT1_ce-gadolinium_T1w", {}),
    ("PSIR sag cervical", "2D", "anat", "anat/acq-cspineSagPSIR_T1w", {}),
    ("T2* axial C2-C3", "2D", "anat", "anat/acq-cspineAxT2star_T2starw", {}),
    ("SAG T2 TSE", "2D", "anat", "anat/acq-cspineSagT2_T2w", {"BodyPartExamined": "CSPINE"}),
    ("SAG STIR", "2D", "anat", "anat/acq-cspineSagSTIR_T2w", {"StudyDescription": "RM COLUNA CERVICAL"}),
    ("sag t2 lombar", "2D", "anat", "anat/acq-lspineSagT2_T2w", {}),
    # junk
    ("Localizer", "2D", "extra_data", "excluded", {}),
    ("3-pl T2* FGRE S", "2D", "anat", "excluded", {}),
    ("ASSET calibration", "3D", "extra_data", "excluded", {}),
    ("Screen Save", "2D", "extra_data", "excluded", {}),
    ("Dose Report", "2D", "extra_data", "excluded", {}),
    ("TOF_3D_multi-slab", "3D", "extra_data", "excluded", {}),
    ("ep2d_perf DSC", "2D", "extra_data", "excluded", {}),
    ("T1 MPR COR", "2D", "anat", "excluded", {}),
    ("MPR Ax T1 pos", "2D", "anat", "excluded", {}),
    ("PosDisp: [3] t2_tse", "2D", "extra_data", "excluded", {}),
    ("relatório", "2D", "extra_data", "excluded", {}),
    ("something new", "2D", "extra_data", "excluded", {}),
]


def run_names(tmp_path, cases):
    dicom = {"participant": {"height": 1, "weight": 2}, "exclude": []}
    for i, (desc, mr, dt, _, hint) in enumerate(cases):
        dicom.setdefault(dt, []).append({
            "provenance": f"case{i}", "attributes": {"SeriesDescription": desc, "MRAcquisitionType": mr},
            "bids": {"acq": "", "ce": None, "rec": None, "run": "<<>>", "part": ["", "mag", "phase", "real", "imag", 0],
                     "suffix": "dwi" if dt == "dwi" else "T1w"}, "meta": {}})
    p = tmp_path / "bidsmap.yaml"
    p.write_text(yaml.safe_dump({"Options": {"plugins": {"dcm2niix2bids": {"args": "-b y"}}}, "DICOM": dicom}))
    braims.BIDSMAP, braims.DATA = p, tmp_path
    hints = {f"case{i}": c[4] for i, c in enumerate(cases)}
    original = naming.dicom_hint
    naming.dicom_hint = lambda path: hints.get(path, {})
    try:
        rows = braims.name_series()
    finally:
        naming.dicom_hint = original
    return {d: (r, n) for d, r, n in rows}, yaml.safe_load(p.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_name(tmp_path, case):
    got, _ = run_names(tmp_path, [case])
    assert got[case[0]][0] == case[3]


def test_spine_is_held_and_labelled(tmp_path):
    _, bm = run_names(tmp_path, [("SAG T2 COLUNA CERVICAL", "2D", "anat", "", {})])
    meta = bm["DICOM"]["anat"][0]["meta"]
    assert meta["BodyPart"] == "CSPINE" and meta["BRAIMSRegion"] == "spine" and "spine" in meta["BRAIMSHold"]


def test_long_sagittal_field_of_view_is_held_for_a_person(tmp_path):
    got, bm = run_names(tmp_path, [("SAG T2 TSE", "2D", "anat", "", {"plane": "sag", "fov_mm": 380})])
    assert "POSSIBLY SPINE" in got["SAG T2 TSE"][1]
    assert bm["DICOM"]["anat"][0]["meta"]["BRAIMSHold"]


def test_brain_is_not_spine():
    for desc in ("t1_mprage_sag_p2_iso", "AX T2 FLAIR", "Cor T2 FS NO", "Órbita T1", "ep2d_diff", "t2_tse_tra",
                 "DTI_32dir", "SAG CUBE FLAIR", "COR STIR NERVIO OPTICO", "colfa"):
        assert naming.spine_region(desc, {"BodyPartExamined": "HEAD", "StudyDescription": "RM CRANIO"})[0] is None, desc


def test_anon_is_enforced_and_height_weight_dropped(tmp_path):
    _, bm = run_names(tmp_path, [("t1_mprage_sag_p2_iso", "3D", "anat", "", {})])
    assert bm["Options"]["plugins"]["dcm2niix2bids"]["anon"] == "y"
    assert "height" not in bm["DICOM"]["participant"] and "weight" not in bm["DICOM"]["participant"]
