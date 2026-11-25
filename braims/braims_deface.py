"""Remove the neck and the face from every image of every session in a BIDS folder (3D, 2D and diffusion).

    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids
    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids --reapply    (after editing a mask in ITK-SNAP)

Per session: the 3D T1 (or another 3D head image) is turned head-up (RAS), whatever order the scanner stored it in,
then gets its neck removed with FSL robustfov and its face with pydeface. Both orbits are kept (eyes, eyelids, orbital fat
and muscles, optic nerves), because they are study data in optic neuritis (MS, NMOSD, MOGAD): each eye is found in the
image itself (the dark vitreous), near where pydeface's registration puts it, and a cone around the eye and back to the
orbital apex is added back to pydeface's face mask. The session is not defaced, and says why, when the registration
stretches the template implausibly, an eye is not found, the nose would stay or the cut is lopsided.
That gives a "keep" mask, which is moved onto every other image of the session, orbit studies included, with a rigid
FSL FLIRT registration, so thick-slice 2D images and diffusion scans lose the same face and neck. Phase images use the
mask of their magnitude image. Each sidecar gets "Defaced": true and "NeckRemoved": true.
Every keep mask (1 = keep, 0 = removed) is saved in bids/derivatives/braims_deface, which never leaves the site,
with a before/after picture of the head surface for each session.
With --reapply, an image whose mask is already saved there gets that mask (for example one corrected in ITK-SNAP).
An image that cannot be defaced is reported and left as it is; the BRAIMS check then keeps it from leaving the site.
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import nibabel as nib
from scipy import ndimage as ndi
from scipy.signal import fftconvolve
from braims_check import image_problems
try:
    import qc                                             # before/after pictures for checking by eye
except ImportError:
    qc = None

args = [a for a in sys.argv[1:] if not a.startswith("--")]
bids = Path(args[0] if args else "/bids")
reapply = "--reapply" in sys.argv
saved = bids / "derivatives" / "braims_deface"
# Eye centres (centroid of the vitreous) and orbital apexes (mm) in the space of pydeface's template (mean_reg2mean.nii.gz)
EYES = np.array(((-29.4, 78.6, -31.2), (28.8, 72.5, -32.7)))
APEXES = EYES + np.array(((12.0, -38.0, 3.0), (-12.0, -38.0, 3.0)))      # 12 mm medial, 38 mm back, 3 mm up
ORBIT_RADIUS = (24.0, 16.0)  # mm kept around the eye centre (12 mm beyond a normal eyeball) and at the apex
SCALES = (0.75, 1.33)        # plausible stretch of the template onto a head, along each axis
EYE_SEARCH = 12.0            # mm from the registration's eye centre within which the eye must be found
EYE_CONTRAST = 0.3           # minimum (ring - vitreous) / bright tissue for an eye to count as found (eyes: 0.45-0.53)
NOSE_LEFT = 1.0              # cm3 of tissue allowed in front of the eyes below them, near the midline
LOPSIDED = (0.67, 1.5)       # allowed ratio of tissue removed left / right


def run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def sidecar(img):
    side = Path(str(img)[:-7] + ".json")
    return side, (json.loads(side.read_text()) if side.exists() else {})


def voxels_near(shape, affine, centre, radius):
    """Voxel indices (3, n) and their positions (mm) in the box that holds a ball of radius mm around centre."""
    corners = np.array(np.meshgrid(*[[-1, 1]] * 3)).reshape(3, -1).T * radius + centre
    v = nib.affines.apply_affine(np.linalg.inv(affine), corners)
    lo = np.clip(np.floor(v.min(0)).astype(int), 0, shape[:3])
    hi = np.clip(np.ceil(v.max(0)).astype(int) + 1, 0, shape[:3])
    ijk = np.indices(hi - lo).reshape(3, -1) + lo[:, None]
    return ijk, affine[:3, :3] @ ijk + affine[:3, 3:4]


def orbit_cones(shape, affine, eyes, apexes):
    """Both orbits: a cone from each eye centre (ORBIT_RADIUS[0], also in front of the eye) to its apex (ORBIT_RADIUS[1])."""
    keep = np.zeros(shape[:3], bool)
    r_eye, r_apex = ORBIT_RADIUS
    for g, a in zip(eyes, apexes):
        ijk, xyz = voxels_near(shape, affine, (g + a) / 2, np.linalg.norm(a - g) / 2 + r_eye)
        v = a - g
        u = np.clip(((xyz - g[:, None]) * v[:, None]).sum(0) / (v @ v), 0, 1)
        inside = np.linalg.norm(xyz - (g[:, None] + u * v[:, None]), axis=0) <= r_eye + (r_apex - r_eye) * u
        keep[tuple(ijk[:, inside])] = True
    return keep


def find_eyes(img, expected):
    """Each eye in the image: the centre of the darkest 8 mm ball (vitreous) ringed by brighter tissue (13-17 mm),
    within EYE_SEARCH mm of where the registration puts it. Returns the centres (mm), their contrast and how far they moved."""
    data = np.asanyarray(img.dataobj).astype(np.float32)
    grid = np.mgrid[-32:33, -32:33, -32:33].astype(np.float32)
    dist = np.sqrt((grid ** 2).sum(0))
    ball, ring = (dist <= 8).astype(np.float32), ((dist >= 13) & (dist <= 17)).astype(np.float32)
    ball, ring = ball / ball.sum(), ring / ring.sum()
    centres, contrast, moved = [], [], []
    for e in expected:
        pts = nib.affines.apply_affine(np.linalg.inv(img.affine), grid.reshape(3, -1).T + e)
        cube = ndi.map_coordinates(data, pts.T, order=1, cval=0).reshape(dist.shape)
        cube /= max(np.percentile(cube, 95), 1e-6)
        score = np.where(dist <= EYE_SEARCH, fftconvolve(cube, ring, mode="same") - fftconvolve(cube, ball, mode="same"), -np.inf)
        i = np.unravel_index(np.argmax(score), score.shape)
        centres.append(e + np.array(i) - 32)
        contrast.append(float(score[i]))
        moved.append(float(dist[i]))
    return np.array(centres), contrast, moved


def head(data):
    """Head mask: smoothed image above a third of the Otsu threshold, largest piece, holes filled."""
    sm = ndi.gaussian_filter(data.astype(np.float32), 1.2)
    hist, edges = np.histogram(sm[sm > 0], 256)
    c = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    m0, m1 = np.cumsum(hist * c) / np.maximum(w0, 1), (np.sum(hist * c) - np.cumsum(hist * c)) / np.maximum(w0[-1] - w0, 1)
    lab, n = ndi.label(sm > 0.35 * c[np.argmax(w0 * (w0[-1] - w0) * (m0 - m1) ** 2)])
    return ndi.binary_fill_holes(lab == np.argmax(np.bincount(lab.ravel())[1:]) + 1) if n else lab > 0


def cut_checks(img, tissue, face, orbits, eyes, apexes):
    """Tissue left where the nose is (cm3) and tissue removed left / right, in the head's own frame from the eyes and
    apexes, so a tilted head is measured the same way as a straight one."""
    mid = eyes.mean(0)
    front = (eyes - apexes).mean(0)
    front /= np.linalg.norm(front)
    side = eyes[1] - eyes[0]
    side -= (side @ front) * front
    side /= np.linalg.norm(side)
    up = np.cross(side, front)
    xyz = img.affine[:3, :3] @ np.indices(tissue.shape).reshape(3, -1) + img.affine[:3, 3:4] - mid[:, None]
    s, f, u = [(v @ xyz).reshape(tissue.shape) for v in (side, front, up)]
    nose = (np.abs(s) < 15) & (f > 12) & (u > -55) & (u < -10) & ~orbits
    left_cc = (tissue & face & nose).sum() * abs(np.linalg.det(img.affine[:3, :3])) / 1000
    removed = tissue & ~(face | orbits)
    return left_cc, removed[s < 0].sum() / max(removed[s >= 0].sum(), 1)


def landmarks(out):
    """pydeface's template with the eye centres (1, 2) and orbital apexes (3, 4) marked as 4 mm balls."""
    import pydeface
    fm = nib.load(Path(pydeface.__file__).parent / "data" / "facemask.nii.gz")
    marks = np.zeros(fm.shape, np.uint8)
    for k, c in enumerate(list(EYES) + list(APEXES), 1):
        ijk, xyz = voxels_near(fm.shape, fm.affine, c, 4)
        inside = ((xyz - c[:, None]) ** 2).sum(0) <= 16
        marks[tuple(ijk[:, inside])] = k
    nib.Nifti1Image(marks, fm.affine).to_filename(out)
    return out


def face_and_neck(ref, tmp):
    """Keep mask on the reference image's own grid, or None and the reason it was not made."""
    canon = nib.as_closest_canonical(nib.load(ref))
    up, crop, mat = tmp / "headup.nii.gz", tmp / "crop.nii.gz", tmp / "roi2full.mat"
    canon.to_filename(up)                                 # FLIRT starts from the voxel axes: work head-up, like the template
    run("robustfov", "-i", up, "-r", crop, "-m", mat)
    run("pydeface", crop, "--outfile", tmp / "crop_defaced.nii.gz", "--force", "--nocleanup")
    scales = np.linalg.svd(np.loadtxt(tmp / "crop_pydeface.mat")[:3, :3], compute_uv=False)
    run("flirt", "-in", MARKS, "-ref", crop, "-applyxfm", "-init", tmp / "crop_pydeface.mat",
        "-interp", "nearestneighbour", "-out", tmp / "marks.nii.gz")
    cropimg, marks = nib.load(crop), nib.load(tmp / "marks.nii.gz")
    m = np.asanyarray(marks.dataobj)
    points = np.array([nib.affines.apply_affine(marks.affine, np.argwhere(m == k).mean(0)) if (m == k).any()
                       else np.full(3, np.nan) for k in (1, 2, 3, 4)])
    if not np.isfinite(points).all():                     # the template's eyes fell outside the image
        return None, "the eyes were not found where the registration puts them"
    expected, apexes = points[:2], points[2:]
    eyes, contrast, moved = find_eyes(cropimg, expected)
    face = np.asanyarray(nib.load(tmp / "crop_pydeface_mask.nii.gz").dataobj) > 0.5
    apexes = apexes + eyes - expected
    orbits = orbit_cones(cropimg.shape, cropimg.affine, eyes, apexes)
    keep = face | orbits
    # checks: a wrong registration moves the cut off the face
    problem = None
    gap = np.linalg.norm(eyes[0] - eyes[1])
    if not (SCALES[0] <= scales.min() and scales.max() <= SCALES[1]):
        problem = f"the template had to be stretched {scales.min():.2f} to {scales.max():.2f} times: the image may be distorted"
    elif min(contrast) < EYE_CONTRAST or max(moved) > EYE_SEARCH - 2 or not 45 <= gap <= 80:
        problem = "the eyes were not found where the registration puts them"
    else:
        left_cc, ratio = cut_checks(cropimg, head(np.asanyarray(cropimg.dataobj)), face, orbits, eyes, apexes)
        if left_cc > NOSE_LEFT:
            problem = f"the nose would stay ({left_cc:.0f} cm3 left in front of the eyes)"
        elif not LOPSIDED[0] <= ratio <= LOPSIDED[1]:
            problem = f"the cut is lopsided ({ratio:.1f} times more removed on one side)"
    nib.Nifti1Image(keep.astype(np.uint8), cropimg.affine).to_filename(tmp / "crop_keep.nii.gz")
    run("flirt", "-in", tmp / "crop_keep.nii.gz", "-ref", up, "-applyxfm", "-init", mat,
        "-interp", "nearestneighbour", "-out", tmp / "headup_keep.nii.gz")
    k = nib.load(tmp / "headup_keep.nii.gz")
    native = nib.load(ref)
    back = nib.orientations.ornt_transform(nib.io_orientation(k.affine), nib.io_orientation(native.affine))
    k = nib.orientations.apply_orientation(np.asanyarray(k.dataobj) > 0.5, back)
    nib.Nifti1Image(k.astype(np.uint8), native.affine).to_filename(tmp / "keep_ref.nii.gz")
    return tmp / "keep_ref.nii.gz", problem


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
MARKS = landmarks(Path(tempfile.mkdtemp()) / "orbit_landmarks.nii.gz")
sessions = sorted({p.parent.parent for d in ("anat", "dwi") for p in bids.glob(f"sub-*/**/{d}/*.nii.gz")})
for session in sessions:
    images = [p for d in ("anat", "dwi") for p in sorted((session / d).glob("*.nii.gz"))]
    todo = [p for p in images if sidecar(p)[1].get("Defaced") is not True]
    if not todo:
        continue
    print(session.relative_to(bids))
    problems = {img: [kind for kind, _ in image_problems(img)] for img in images}
    held = [img for img in images if {"copy", "stretched"} & set(problems[img])]
    if held:
        # a stretched image or a second copy can give a wrong mask: sort them out first, then deface the whole session
        print("  NOT DEFACED  the whole session: first sort out these images (Step 6 and Part E of the manual)")
        for img in held:
            print(f"    {img.relative_to(session)}  ({', '.join(k for k in problems[img] if k != 'one slice')})")
        failed += todo
        continue
    for img in list(todo):
        if "one slice" in problems[img]:
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
    picture = saved / session.relative_to(bids) / f"{'_'.join(session.relative_to(bids).parts)}_deface_check.png"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. Neck (robustfov) and face (pydeface, orbits kept) on the reference image -> a keep mask on its own grid
        original = tmp / "reference.nii.gz"
        shutil.copyfile(ref, original)                    # register from the untouched copy
        try:
            keep_ref, problem = face_and_neck(original, tmp)
        except subprocess.CalledProcessError as e:
            keep_ref, problem = None, f"neck and face removal failed on {ref.name} ({e.stderr.decode(errors='replace').strip()[-200:]})"
        if problem:
            print(f"  NOT DEFACED  the whole session: {problem}. See Part E of the manual.")
            if keep_ref and qc:                           # show the cut that was refused
                trial = tmp / "trial.nii.gz"
                shutil.copyfile(original, trial)
                im = nib.load(trial)
                nib.Nifti1Image(np.asanyarray(im.dataobj) * (np.asanyarray(nib.load(keep_ref).dataobj) > 0), im.affine).to_filename(trial)
                picture = picture.with_name(picture.stem + "_REFUSED.png")
                picture.parent.mkdir(parents=True, exist_ok=True)
                qc.before_after(original, trial, picture)
                print(f"  picture of the refused cut: {picture.relative_to(bids)}")
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
