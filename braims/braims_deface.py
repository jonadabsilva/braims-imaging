"""Remove the neck and the face from every image of every session in a BIDS folder (3D, 2D and diffusion).

    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids
    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids --reapply    (after editing a mask in ITK-SNAP)

Per session: the 3D T1 (or another 3D head image) gets its neck removed with FSL robustfov and its face with pydeface.
pydeface uses its own face mask with both orbits added back (eyes, eyelids, orbital fat and muscles, optic nerves),
because the orbits are study data in optic neuritis (MS, NMOSD, MOGAD). That gives a "keep" mask, which is moved onto
every other image of the session, orbit studies included, with a rigid FSL FLIRT registration, so thick-slice 2D images
and diffusion scans lose the same face and neck. Phase images use the mask of their magnitude image.
Each sidecar gets "Defaced": true and "NeckRemoved": true.
Every keep mask (1 = keep, 0 = removed) is saved in bids/derivatives/braims_deface, which never leaves the site,
with a before/after picture of the head surface for each session.
With --reapply, an image whose mask is already saved there gets that mask (for example one corrected in ITK-SNAP).
An image that cannot be defaced is reported and left as it is; the BRAIMS check then keeps it from leaving the site.
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import nibabel as nib
try:
    import qc                                             # before/after pictures for checking by eye
except ImportError:
    qc = None

args = [a for a in sys.argv[1:] if not a.startswith("--")]
bids = Path(args[0] if args else "/bids")
reapply = "--reapply" in sys.argv
saved = bids / "derivatives" / "braims_deface"
# Eye centres (mm) in the space of pydeface's template (mean_reg2mean.nii.gz), found as the centroid of the vitreous
ORBITS = ((-29.4, 78.6, -31.2), (28.8, 72.5, -32.7))
ORBIT_RADIUS = 24.0          # mm around each eye centre: 12 mm beyond a normal eyeball (radius about 12 mm) on every side


def run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def sidecar(img):
    side = Path(str(img)[:-7] + ".json")
    return side, (json.loads(side.read_text()) if side.exists() else {})


def orbit_sparing_facemask(out):
    """pydeface's face mask with both orbits kept: a ball of ORBIT_RADIUS around each eye centre.
    Behind the eyes (optic nerves, orbital apex, chiasm) pydeface removes nothing, so the whole orbit is kept."""
    import pydeface
    fm = nib.load(Path(pydeface.__file__).parent / "data" / "facemask.nii.gz")
    mask = np.asarray(fm.dataobj).copy()
    inv = np.linalg.inv(fm.affine)
    signs = np.array(np.meshgrid([-1, 1], [-1, 1], [-1, 1])).T.reshape(-1, 3)
    for eye in ORBITS:
        centre = np.array(eye)
        vox = (inv @ np.c_[centre + signs * ORBIT_RADIUS, np.ones(8)].T)[:3]
        lo = np.clip(np.floor(vox.min(1)).astype(int), 0, mask.shape)
        hi = np.clip(np.ceil(vox.max(1)).astype(int) + 1, 0, mask.shape)
        ijk = np.indices(hi - lo).reshape(3, -1) + lo[:, None]
        xyz = (fm.affine @ np.vstack([ijk, np.ones(ijk.shape[1])]))[:3]
        inside = ((xyz - centre[:, None]) ** 2).sum(0) <= ORBIT_RADIUS ** 2
        mask[tuple(ijk[:, inside])] = 1
    nib.Nifti1Image(mask, fm.affine, fm.header).to_filename(out)
    return out


def first_volume(img, out):
    im = nib.load(img)
    data = np.asanyarray(im.dataobj)
    nib.Nifti1Image(data[..., 0] if data.ndim == 4 else data, im.affine).to_filename(out)
    return out


def saved_mask(img):
    return saved / img.relative_to(bids).parent / (img.name[:-7] + "_keepmask.nii.gz")


def apply_keep(img, keep):
    im = nib.load(img)
    data = np.asanyarray(im.dataobj)
    km = nib.load(keep)
    k = np.asanyarray(km.dataobj) > 0.5
    out = data * (k[..., None] if data.ndim == 4 else k)
    nib.Nifti1Image(out.astype(data.dtype), im.affine, im.header).to_filename(img)
    target = saved_mask(img)
    if Path(keep) != target:                              # keep a copy of the mask for checking and editing
        target.parent.mkdir(parents=True, exist_ok=True)
        nib.Nifti1Image(k.astype(np.uint8), km.affine).to_filename(target)
    return 100 * (1 - k.mean())


done, failed = 0, []
facemask = orbit_sparing_facemask(Path(tempfile.mkdtemp()) / "facemask_orbits.nii.gz")
sessions = sorted({p.parent.parent for d in ("anat", "dwi") for p in bids.glob(f"sub-*/**/{d}/*.nii.gz")})
for session in sessions:
    images = [p for d in ("anat", "dwi") for p in sorted((session / d).glob("*.nii.gz"))]
    todo = [p for p in images if sidecar(p)[1].get("Defaced") is not True]
    if not todo:
        continue
    print(session.relative_to(bids))
    for img in list(todo):
        shape = nib.load(img).shape
        if len(shape) < 3 or shape[2] < 2:
            print(f"  NOT DEFACED  {img.relative_to(session)}  (one slice: see Part E of the manual)")
            failed.append(img)
            todo.remove(img)
        elif reapply and saved_mask(img).exists():
            removed = apply_keep(img, saved_mask(img))
            side, meta = sidecar(img)
            meta.update({"Defaced": True, "NeckRemoved": True, "DefacingMethod": "saved keep mask (checked or edited by hand)"})
            side.write_text(json.dumps(meta, indent=4))
            todo.remove(img)
            done += 1
            print(f"  done  {img.relative_to(session)}  (saved mask applied, {removed:.0f}% removed)")
    if not todo:
        continue
    three_d = [p for p in images if "/anat/" in p.as_posix() and "part-phase" not in p.name and "acq-orbit" not in p.name
               and sidecar(p)[1].get("MRAcquisitionType") == "3D" and len(nib.load(p).shape) == 3]
    t1 = [p for p in three_d if p.name.endswith("_T1w.nii.gz") and "ce-" not in p.name]
    ref = (t1 or three_d or [None])[0]
    if ref is None:
        print("  no 3D head image in this session: nothing was defaced here. Tell the core center.")
        failed += todo
        continue
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. Neck (robustfov) and face (pydeface) on the reference image -> a keep mask in its own space
        crop, mat, original = tmp / "crop.nii.gz", tmp / "roi2full.mat", tmp / "reference.nii.gz"
        shutil.copyfile(ref, original)                    # register from the untouched copy
        keep_ref = tmp / "keep_ref.nii.gz"
        try:
            run("robustfov", "-i", original, "-r", crop, "-m", mat)
            run("pydeface", crop, "--outfile", tmp / "crop_defaced.nii.gz", "--force", "--nocleanup", "--facemask", facemask)
            run("flirt", "-in", tmp / "crop_pydeface_mask.nii.gz", "-ref", original, "-applyxfm", "-init", mat,
                "-interp", "nearestneighbour", "-out", keep_ref)
        except subprocess.CalledProcessError as e:
            print(f"  NOT DEFACED  the whole session: neck and face removal failed on {ref.relative_to(session)}"
                  f" ({e.stderr.decode(errors='replace').strip()[-200:]})")
            failed += todo
            continue
        # 2. The same mask on every other image of the session
        masks = {}
        for img in todo:
            mag = Path(str(img).replace("part-phase", "part-mag"))
            how = "robustfov + pydeface, orbits kept" + ("" if img == ref else f", on {ref.name}, moved here with FSL FLIRT (6 dof)")
            try:
                if img == ref:
                    keep = keep_ref
                elif "part-phase" in img.name and (mag in masks or saved_mask(mag).exists()):
                    keep = masks.get(mag, saved_mask(mag))      # the magnitude image of the same scan
                elif "part-phase" in img.name:
                    # phase without its magnitude: too little anatomy to register, so use the scanner coordinates alone
                    keep = tmp / f"{img.name[:-7]}_keep.nii.gz"
                    run("flirt", "-in", keep_ref, "-ref", first_volume(img, tmp / f"{img.name[:-7]}_vol0.nii.gz"),
                        "-applyxfm", "-usesqform", "-interp", "nearestneighbour", "-out", keep)
                    how = f"robustfov + pydeface, orbits kept, on {ref.name}, moved here by the scanner coordinates (FSL FLIRT)"
                else:
                    target = first_volume(img, tmp / f"{img.name[:-7]}_vol0.nii.gz")
                    omat, keep = tmp / f"{img.name[:-7]}.mat", tmp / f"{img.name[:-7]}_keep.nii.gz"
                    # same session, same scanner coordinates: start from the header geometry, no wide rotation search
                    run("flirt", "-in", original, "-ref", target, "-dof", "6", "-cost", "mutualinfo", "-usesqform", "-nosearch",
                        "-omat", omat)
                    run("flirt", "-in", keep_ref, "-ref", target, "-applyxfm", "-init", omat,
                        "-interp", "nearestneighbour", "-out", keep)
            except subprocess.CalledProcessError as e:
                print(f"  NOT DEFACED  {img.relative_to(session)}  ({e.stderr.decode(errors='replace').strip()[-200:]})")
                failed.append(img)
                continue
            masks[img] = keep
            removed = apply_keep(img, keep)
            if img == ref and qc:
                picture = saved / session.relative_to(bids) / f"{'_'.join(session.relative_to(bids).parts)}_deface_check.png"
                qc.before_after(original, img, picture)
                print(f"  check picture: {picture.relative_to(bids)}")
            side, meta = sidecar(img)
            meta.update({"Defaced": True, "NeckRemoved": True, "DefacingMethod": how})
            side.write_text(json.dumps(meta, indent=4))
            done += 1
            print(f"  done  {img.relative_to(session)}  ({removed:.0f}% of the volume removed)")
print(f"\n{done} image(s) defaced. Look at one image per session before you go on.")
if failed:
    print(f"{len(failed)} image(s) could not be defaced. They stay at the site: the BRAIMS check will not package them.")
