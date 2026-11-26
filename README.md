# BRAIMS imaging pipeline

Prepares brain MRI at each BRAIMS site for sharing: it sorts the DICOM export, names every series by the network's
rules, converts to BIDS, removes the neck and face from every image (3D, 2D and diffusion) while keeping both orbits,
checks that nothing identifies the person, the date or the hospital, and builds a checksummed upload package. Everything runs on a
computer inside the hospital. Only the checked package leaves it.

There are two ways to run it:

| | One-click (Docker) | Step by step |
|---|---|---|
| Install | Docker Desktop | Docker Desktop, Python 3.12, BIDScoin 4.6.2 |
| Naming | automatic, from `braims/naming_rules.csv` | reviewed by hand in the BIDScoin editor |
| Per exam | copy the exam into `raw`, double-click **Run**, read the report, double-click **Package** | nine commands, described in the site manual |

Both use the same image, `ghcr.io/jonadabsilva/braims`, and produce the same names, the same defacing and the same
checks. The BRAIMS site manual describes both routes, with screenshots and a troubleshooting guide.

## One-click quick start

1. Install Docker Desktop and start it.
2. Make a BRAIMS folder on a local disk (for example `C:\BRAIMS` or `~/BRAIMS`; not inside OneDrive, Dropbox, Google
   Drive or iCloud, which the launchers refuse) and put the three launchers for your system in it
   (`launchers/windows/*.bat`, `launchers/mac/*.command` or `launchers/linux/*.sh`).
3. Copy each exam into `raw/sub-<code>/ses-<nn>` (for example `raw/sub-BR010001/ses-01`). Write the code next to the
   person's name in your site key file, which stays at the site.
4. Double-click **BRAIMS_Run**. The first run downloads the image (about 1 GB; 3.6 GB on disk). Run first checks the
   exam folders (BRAIMS codes, `ses-nn` names, one person per exam, free disk space) and stops, changing nothing,
   if one is wrong.
5. Open the newest file in `reports`. A second person reads it.
6. Double-click **BRAIMS_Package**. Send the `.zip` and its `.sha256` from `outbox`.

If a check picture shows a face, or tissue that should have stayed was removed, correct the mask in ITK-SNAP
(`bids/derivatives/braims_deface/.../*_keepmask.nii.gz`) and double-click **BRAIMS_Reapply**: the corrected mask is
applied to the image as it was converted (kept at the site), so tissue removed by mistake comes back.

```
BRAIMS/
  raw/sub-BR010001/ses-01/...   DICOM, one folder per coded exam      (never leaves the site)
  bids/                         converted data, made by Run
  bids/derivatives/             defacing masks, check pictures and the images as converted, before defacing
                                                                      (never leaves the site)
  reports/                      one report per run, shows faces       (never leaves the site)
  outbox/                       upload packages
  naming_rules_local.csv        optional extra naming rules from the core center
```

## What each step does

| Step | Tool |
|---|---|
| Sort each exam into one folder per series | `dicomsort` (BIDScoin) |
| Read one image per series and propose names | `bidsmapper` (BIDScoin) |
| Check the exam folders: BRAIMS codes, session names, one person per exam, free disk space | `braims/braims_meta.py` |
| Apply the BRAIMS naming rules (accents and case ignored; Siemens, GE, Philips and Canon names); anything not recognized is excluded and flagged; spine series are recognized and held at the site | `braims/braims_naming.py` with `braims/naming_rules.csv` |
| Convert DICOM to BIDS (NIfTI + JSON sidecars), ages rounded and dates replaced (BIDScoin `anon`, set by BRAIMS) | `bidscoiner` (BIDScoin, dcm2niix) |
| Repair what the export or the converter left wrong: stretched images, extra copies, broken headers, scanner mosaics | `braims/braims_repair.py` |
| Turn each session's 3D T1 head-up, remove the neck (FSL robustfov) and the face (pydeface's face mask) while keeping both orbits, then move that mask onto every other image of the session, orbit studies included (FSL FLIRT, 6 dof) | `braims/braims_deface.py` |
| Keep what analysis needs without identifying anyone: a scanner ID (salted hash of the serial number, the same for every exam on one scanner) and the days between each person's sessions | `braims/braims_meta.py` |
| Let out only what is on a list: BIDS images, sidecars and tables (any other file, DICOM recognized by its content, is an error); acquisition fields in the sidecars (anything else is removed; words in series names that are not MRI terms are masked); no text in the image headers; no dates; damaged images refused | `braims/braims_check.py --fix` |
| Zip the dataset with a SHA-256 manifest, leaving out `code/`, `derivatives/` and held images, which stay at the site | `braims/braims_check.py --package` |

The orbits are kept because they are study data (optic neuritis in MS, NMOSD and MOGAD). Each eye is found in the
person's own 3D T1 (the dark vitreous, within 12 mm of where pydeface's registration puts it), and a cone from the eye
back to the orbital apex is added to pydeface's face mask: 24 mm around the eye centre (12 mm beyond a normal eyeball),
narrowing to 16 mm at the apex (`EYES`, `APEXES` and `ORBIT_RADIUS` in `braims/braims_deface.py`). In front of the
eye the cone stops 3 mm beyond the globe (`GLOBE_FRONT`), so the eyelids are removed with the rest of the face. The
nose, mouth, cheeks, chin and neck are removed.

Some images stay at the site, listed in the report (`BRAIMSHold` in the sidecar): spine series, until a validated
spine defacing exists; series that only possibly show the spine (a long sagittal field of view), until a person
decides; and sessions with no head image to place the face mask on (orbit studies alone).

Every image is defaced: each step has a fallback, so that no image stays behind because of the tool.

- The image is turned head-up (RAS), the way pydeface's template is stored, before the neck and face are cut: FLIRT
  starts from the image axes, and scanners save sagittal and coronal slices in other orders.
- The face mask is placed in up to three ways: by pydeface's own registration; if its result fails the checks, from the
  eyes found anywhere in the image and the centre of the head; if that fails too, from the outline of the head. A
  result passes when both eyes are found, no tissue is left where the nose is, and the two sides lose about the same.
  If none passes, the best one is used with a wider region kept around the orbits, and the session is marked for a look
  at its check picture (`DefacingNote` in the sidecars, a warning in the check).
- An image that cannot be registered to the session's 3D T1 gets the mask by the scanner coordinates alone. Diffusion
  images get the mask with 5 mm more kept around it, because their geometry is distorted near the orbits.
- Before defacing, `braims_repair.py` uses the converter's evenly resampled copy in place of an image stretched by
  missing slices, gives every extra copy of a series a valid name (`rec-copy2`, ...), repairs an orientation matrix
  that does not fit the voxel size, and unpacks a Siemens mosaic that the converter left as one flat picture because
  the export had lost the private header. Each repair is written in the sidecar (`ImageRepair`).

Tested on four 3D T1s (one volunteer, three sessions of one patient), each stored axial, sagittal, coronal and tilted
20 degrees: the three storage orders gave identical masks; in all 32 runs both globes and the orbit contents were kept
whole (10.5 to 11.6 mm beyond the globe) and the nose was removed. Placement from the eyes alone located them within
1.3 mm of the registration-based result on all eight heads tested. The mosaic unpacking reproduced dcm2niix's own result
voxel for voxel, with the same affine, on a scan available both ways.

In the step-by-step route, defacing runs from the same image:
`docker run --rm -v C:\BRAIMS:/data ghcr.io/jonadabsilva/braims:1.1.0 deface`. Masks can be corrected by hand in
ITK-SNAP and applied again with `deface --reapply`.

Tests: `python -m pytest tests` (naming of 70+ real series names, the check against planted identifiers, repairs,
defacing logic, pre-flight). End to end with the real tools: `tests/make_exams.py` builds DICOM exams from TemplateFlow
heads (adult, child, tilted, with spine, orbits only), then `tests/e2e_assert.py` checks the result (see the files).

To build the image yourself: `docker build -f docker/Dockerfile -t braims .`

## Versions

BIDScoin 4.6.2 (base image `marcelzwiers/bidscoin:4.6.2`, which includes dcm2niix and pydeface), FSL robustfov
(`fsl-bet2` from the FSL conda channel). Each released image is tagged with the version of this repository.

## Licence

The BRAIMS code is under the MIT licence (`LICENSE`). The image also contains BIDScoin (GPL-3.0), dcm2niix
(BSD-style), pydeface (MIT) and parts of FSL, which are free for non-commercial use only under the FSL licence.

## How to cite

Please cite the BRAIMS paper (also in `CITATION.cff`):

- Carnero Contentti E, Alonso R, Boldrini V, et al. Collaborative Latin American Imaging Network In Neuroimmunology
  Research (BRAIMS): rationale, structure, challenges, and future directions. Mult Scler Relat Disord. 2026;110:107203.
  doi:10.1016/j.msard.2026.107203

A paper describing this pipeline is in preparation; this section will be updated when it is published.
Please also cite the tools the pipeline runs:

- Zwiers MP, Moia S, Oostenveld R. BIDScoin: a user-friendly application to convert source data to the Brain Imaging
  Data Structure. Front Neuroinform. 2021;15:770608. doi:10.3389/fninf.2021.770608
- Li X, Morgan PS, Ashburner J, Smith J, Rorden C. The first step for neuroimaging data analysis: DICOM to NIfTI
  conversion. J Neurosci Methods. 2016;264:47–56. doi:10.1016/j.jneumeth.2016.03.001
- Gulban OF, et al. poldracklab/pydeface: v2.0.0. Zenodo; 2019. doi:10.5281/zenodo.3524401
- Jenkinson M, Beckmann CF, Behrens TEJ, Woolrich MW, Smith SM. FSL. NeuroImage. 2012;62(2):782–790.
  doi:10.1016/j.neuroimage.2011.09.015
