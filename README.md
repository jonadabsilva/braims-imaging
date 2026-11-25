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
2. Make a BRAIMS folder (for example `C:\BRAIMS` or `~/BRAIMS`) and put the two launchers for your system in it
   (`launchers/windows/*.bat` or `launchers/mac/*.command`).
3. Copy each exam into `raw/sub-<code>/ses-<nn>` (for example `raw/sub-BR010001/ses-01`). Write the code next to the
   person's name in your site key file, which stays at the site.
4. Double-click **BRAIMS_Run**. The first run downloads the image (about 1 GB; 3.6 GB on disk).
5. Open the newest file in `reports`. A second person reads it.
6. Double-click **BRAIMS_Package**. Send the `.zip` and its `.sha256` from `outbox`.

```
BRAIMS/
  raw/sub-BR010001/ses-01/...   DICOM, one folder per coded exam      (never leaves the site)
  bids/                         converted data, made by Run
  bids/derivatives/             defacing masks and check pictures     (never leaves the site)
  reports/                      one report per run, shows faces       (never leaves the site)
  outbox/                       upload packages
  naming_rules_local.csv        optional extra naming rules from the core center
```

## What each step does

| Step | Tool |
|---|---|
| Sort each exam into one folder per series | `dicomsort` (BIDScoin) |
| Read one image per series and propose names | `bidsmapper` (BIDScoin) |
| Apply the BRAIMS naming rules; anything not recognized is excluded and flagged | `braims/braims.py` with `braims/naming_rules.csv` |
| Convert DICOM to BIDS (NIfTI + JSON sidecars), ages rounded and dates replaced | `bidscoiner` (BIDScoin, dcm2niix) |
| Remove the neck (FSL robustfov) and the face (pydeface, with its face mask modified to keep both orbits) on each session's 3D T1, then move that mask onto every other image of the session, orbit studies included (FSL FLIRT, 6 dof) | `braims/braims_deface.py` |
| Remove identifying sidecar fields, header text and dates; refuse DICOM, wrong codes, converter copies, one-slice images (unpacked scanner mosaics) and images that were not defaced | `braims/braims_check.py --fix` |
| Zip the dataset with a SHA-256 manifest, leaving out `code/` and `derivatives/`, which stay at the site | `braims/braims_check.py --package` |

The orbits are kept because they are study data (optic neuritis in MS, NMOSD and MOGAD). The orbit regions are two
balls of 24 mm radius around the eye centres, 12 mm beyond a normal eyeball on every side, defined in the space of
pydeface's template (`ORBITS` and `ORBIT_RADIUS` in `braims/braims_deface.py`) and added to pydeface's face mask;
pydeface's own registration then moves them onto each person. The margin protects the orbit if that registration is
off by several millimetres. The eyelids and the skin around the eyes therefore stay in the images, a small part of the
face.

In the step-by-step route, defacing runs from the same image:
`docker run --rm -v C:\BRAIMS:/data ghcr.io/jonadabsilva/braims:1.0.0 deface`. Masks can be corrected by hand in
ITK-SNAP and applied again with `deface --reapply`.

To build the image yourself: `docker build -f docker/Dockerfile -t braims .`

## Versions

BIDScoin 4.6.2 (base image `marcelzwiers/bidscoin:4.6.2`, which includes dcm2niix and pydeface), FSL robustfov
(`fsl-bet2` from the FSL conda channel). Each released image is tagged with the version of this repository.

## Licence

The BRAIMS code is under the MIT licence (`LICENSE`). The image also contains BIDScoin (GPL-3.0), dcm2niix
(BSD-style), pydeface (MIT) and parts of FSL, which are free for non-commercial use only under the FSL licence.

## How to cite

See `CITATION.cff`, and cite the tools the pipeline runs:

- Zwiers MP, Moia S, Oostenveld R. BIDScoin: a user-friendly application to convert source data to the Brain Imaging
  Data Structure. Front Neuroinform. 2021;15:770608. doi:10.3389/fninf.2021.770608
- Li X, Morgan PS, Ashburner J, Smith J, Rorden C. The first step for neuroimaging data analysis: DICOM to NIfTI
  conversion. J Neurosci Methods. 2016;264:47–56. doi:10.1016/j.jneumeth.2016.03.001
- Gulban OF, et al. poldracklab/pydeface: v2.0.0. Zenodo; 2019. doi:10.5281/zenodo.3524401
- Jenkinson M, Beckmann CF, Behrens TEJ, Woolrich MW, Smith SM. FSL. NeuroImage. 2012;62(2):782–790.
  doi:10.1016/j.neuroimage.2011.09.015
