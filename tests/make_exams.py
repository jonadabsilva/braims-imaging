"""Build a BRAIMS folder of synthetic DICOM exams covering the scenarios of the review (end-to-end tests).

    python tests/make_exams.py <heads_dir> <braims_folder> [scenario ...]

heads_dir holds TemplateFlow heads with faces (see tests/README.md for the download commands).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from synth_dicom import write_series

ADULT_T1 = "tpl-MNI152NLin2009cAsym_res-01_T1w.nii.gz"
ADULT_T2 = "tpl-MNI152NLin2009cAsym_res-01_T2w.nii.gz"
OTHER_T1 = "tpl-MNI152NLin6Asym_res-01_T1w.nii.gz"
CHILD_T1 = "tpl-MNIPediatricAsym_cohort-1_res-1_T1w.nii.gz"
CHILD_T2 = "tpl-MNIPediatricAsym_cohort-1_res-1_T2w.nii.gz"
ORBIT_CENTRE = (0.0, -45.0, -38.0)          # LPS mm, between the globes of the MNI heads


def standard_brain(h, ses, t1=ADULT_T1, t2=ADULT_T2, scale=0.25, study=None, patient=None, n=0):
    s = dict(study or {})
    write_series(h / t1, ses / "s01", "t1_mprage_sag_p2_iso", "sag", 1.0, fov=(256, 256, 176), intensity=scale,
                 series_number=n + 1, study=s, patient=patient)
    write_series(h / t2, ses / "s02", "t2_tse_tra", "ax", 0.9, thickness=4, gap=0.4, fov=(230, 230, 150),
                 intensity=scale, series_number=n + 2, study=s, patient=patient, mr_type="2D")
    write_series(h / t2, ses / "s03", "t2_tirm_tra_dark-fluid", "ax", 0.9, thickness=4, gap=0.4, fov=(230, 230, 150),
                 intensity=scale * 0.8, series_number=n + 3, study=s, patient=patient, mr_type="2D")


def orbit(h, ses, desc="ÓRBITAS STIR COR", t2=ADULT_T2, scale=0.25, study=None, patient=None, n=10):
    write_series(h / t2, ses / f"s{n}", desc, "cor", 0.5, thickness=3, fov=(160, 160, 60), center=ORBIT_CENTRE,
                 intensity=scale, series_number=n, study=study, patient=patient, mr_type="2D")


def spine(h, ses, desc="SAG T2 COLUNA CERVICAL", t2=ADULT_T2, scale=0.25, study=None, patient=None, n=20, body="CSPINE"):
    # the MNI heads end at the mouth: a sagittal slab low and posterior stands in for a cervical spine field of view
    write_series(h / t2, ses / f"s{n}", desc, "sag", 0.6, thickness=3, gap=0.3, fov=(240, 240, 45),
                 center=(0.0, 0.0, -60.0), intensity=scale, series_number=n, study=study, patient=patient,
                 mr_type="2D", extra={"BodyPartExamined": body})


SCENARIOS = {
    # sub, ses: builder
    "adult_full": ("sub-BR010001", "ses-01", lambda h, s: (standard_brain(h, s), orbit(h, s),
                   orbit(h, s, "COR STIR NERVIO OPTICO", n=11))),
    "adult_followup": ("sub-BR010001", "ses-02", lambda h, s: standard_brain(h, s, study={"date": "20200901"})),
    "other_head_tilted": ("sub-BR010002", "ses-01", lambda h, s: write_series(
        h / OTHER_T1, s / "s01", "SAG FSPGR BRAVO", "sag", 1.0, fov=(256, 256, 180), tilt=20, intensity=0.25,
        study={"manufacturer": "GE MEDICAL SYSTEMS", "model": "SIGNA Premier", "date": "20210310"})),
    "child": ("sub-BR010003", "ses-01", lambda h, s: (standard_brain(h, s, CHILD_T1, CHILD_T2, scale=25,
              study={"date": "20220105"}),)),
    "with_spine": ("sub-BR010004", "ses-01", lambda h, s: (standard_brain(h, s, study={"date": "20230220"}),
                   spine(h, s, study={"date": "20230220"}), spine(h, s, "sag stir medula", n=21, study={"date": "20230220"}))),
    "orbit_only": ("sub-BR010005", "ses-01", lambda h, s: (orbit(h, s, study={"date": "20230301"}),
                   orbit(h, s, "Órbita T1 FS pós", n=12, study={"date": "20230301"}))),
    "two_2d_only": ("sub-BR010006", "ses-01", lambda h, s: (
        write_series(h / ADULT_T2, s / "s01", "Ax T2 FSE", "ax", 0.9, thickness=5, gap=1, fov=(230, 230, 160),
                     intensity=0.25, mr_type="2D", study={"date": "20230401"}),
        write_series(h / ADULT_T1, s / "s02", "Ax T1 SE", "ax", 0.9, thickness=5, gap=1, fov=(230, 230, 160),
                     intensity=0.25, mr_type="2D", series_number=2, study={"date": "20230401"}))),
    "missing_slices": ("sub-BR010007", "ses-01", lambda h, s: (standard_brain(h, s, study={"date": "20230501"}),
                       write_series(h / ADULT_T2, s / "s09", "t2_tse_tra_repeat", "ax", 0.9, thickness=4, gap=0.4,
                                    fov=(230, 230, 150), intensity=0.25, mr_type="2D", series_number=9,
                                    drop_slices=(10, 11, 12), study={"date": "20230501"}))),
    "two_patients_mixed": ("sub-BR010008", "ses-01", lambda h, s: (standard_brain(h, s, study={"date": "20230601"}),
                           write_series(h / ADULT_T1, s / "s30", "t1_mprage_sag_p2_iso", "sag", 1.0,
                                        fov=(256, 256, 176), intensity=0.25, series_number=30,
                                        study={"date": "20230601"}, patient={"PatientID": "OTHER999",
                                                                              "PatientName": "OUTRO^PACIENTE"}))),
}


def main():
    heads, folder = Path(sys.argv[1]), Path(sys.argv[2])
    names = sys.argv[3:] or list(SCENARIOS)
    for name in names:
        sub, ses, build = SCENARIOS[name]
        target = folder / "raw" / sub / ses
        build(heads, target)
        print(f"{name}: {target}")


if __name__ == "__main__":
    main()
