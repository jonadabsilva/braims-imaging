"""Remove the neck and the face from every image of every session in a BIDS folder (3D, 2D and diffusion).

    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids
    docker run --rm -v C:\\BRAIMS\\bids:/bids braims/deface:1.0 /bids --reapply    (after editing a mask in ITK-SNAP)

Every image is defaced: each step has a fallback, so no image stays behind because of this tool.
Per session, the images are first repaired where needed (braims_repair.py: stretched images, extra copies, broken
headers, scanner mosaics). Then the 3D T1 (or the best other image) is turned head-up (RAS), whatever order the scanner
stored it in, its neck is removed with FSL robustfov, and the face mask of pydeface is placed on it:
  1. by pydeface's own registration (FSL FLIRT);
  2. if that result fails the checks, from the eyes found anywhere in the image and the centre of the head;
  3. if that fails too, from the outline of the head.
A result passes when both eyes are found in the image (the dark vitreous), no tissue is left where the nose is, and the
two sides lose about the same. The first result that passes is used; if none passes, the best one is used and the session
is marked for a look at its check picture ("DefacingNote" in the sidecars, a warning in the BRAIMS check).
Both orbits are kept (eyes, eyelids, orbital fat and muscles, optic nerves), because they are study data in optic
neuritis (MS, NMOSD, MOGAD): a cone around each eye and back to the orbital apex is added back to the face mask, wider
when the eye itself was not found.
That gives a "keep" mask, which is moved onto every other image of the session, orbit studies included, with a rigid
FSL FLIRT registration, or by the scanner coordinates alone when an image cannot be registered. Phase images use the
mask of their magnitude image. Each sidecar gets "Defaced": true and "NeckRemoved": true.
Every keep mask (1 = keep, 0 = removed) is saved in bids/derivatives/braims_deface, which never leaves the site,
with a before/after picture of the head surface for each session.
With --reapply, an image whose mask is already saved there gets that mask (for example one corrected in ITK-SNAP).
"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
import numpy as np
import nibabel as nib
from scipy import ndimage as ndi
from scipy.signal import fftconvolve
from braims_repair import repair, head, on_grid, volume
try:
    import qc                                             # before/after pictures for checking by eye
except ImportError:
    qc = None

args = [a for a in sys.argv[1:] if not a.startswith("--")]
bids = Path(args[0] if args else "/bids")
reapply = "--reapply" in sys.argv
saved = bids / "derivatives" / "braims_deface"
# In the space of pydeface's template (mean_reg2mean.nii.gz), in mm: the eye centres (centroid of the vitreous), the
# orbital apexes, and the centre and the box of the head above the neck cut (170 mm from the top, as robustfov cuts)
EYES = np.array(((-29.4, 78.6, -31.2), (28.8, 72.5, -32.7)))
APEXES = EYES + np.array(((12.0, -38.0, 3.0), (-12.0, -38.0, 3.0)))      # 12 mm medial, 38 mm back, 3 mm up
HEAD_CENTRE = np.array((-5.4, 5.0, -12.5))
HEAD_BOX = np.array(((-91.2, -94.9, -86.7), (75.7, 121.7, 83.3)))
ORBIT_RADIUS = (24.0, 16.0)  # mm kept around the eye centre (12 mm beyond a normal eyeball) and at the apex
GLOBE_FRONT = 15.0           # mm kept in front of the eye centre: the globe (~12 mm) + 3 mm, so the eyelids are removed
NECK_BEHIND = 15.0           # mm behind the orbital apexes from which the neck is kept below the neck cut (brainstem,
                             # upper cervical cord, vertebrae); everything in front of it (jaw, mouth, chin) is removed
BRAIN_CUT_CC = 2.0           # cm3 of brain (FSL bet2) inside the removed region above which a person must look
LID_RADIUS = 22.0            # mm around each eye's axis in which everything in front of the globe is removed (eyelids,
                             # brow skin): pydeface's own mask keeps the upper face down to just below the eyes
EPI_MARGIN = 5.0             # mm added to the keep mask of diffusion images (EPI distortion near the orbits and forehead)
DEFACE_VERSION = "1.1.0"
ORBIT_UNSURE = 8.0           # mm added to both when the eye itself was not found in the image
SCALES = (0.75, 1.33)        # plausible stretch of the template onto a head, along each axis
EYE_SEARCH = 12.0            # mm from the expected eye centre within which the eye must be found
EYE_CONTRAST = 0.3           # minimum (ring - vitreous) / bright tissue for an eye to count as found (eyes: 0.45-0.53)
BRIGHT_EYES = ("_T2w", "_T2starw", "_PDT2", "_dwi")   # images where the vitreous is bright instead of dark
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


def orbit_cones(shape, affine, eyes, apexes, extra=0.0):
    """Both orbits: a cone from each eye centre (ORBIT_RADIUS[0], also in front of the eye) to its apex (ORBIT_RADIUS[1])."""
    keep = np.zeros(shape[:3], bool)
    r_eye, r_apex = ORBIT_RADIUS[0] + extra, ORBIT_RADIUS[1] + extra
    for g, a in zip(eyes, apexes):
        ijk, xyz = voxels_near(shape, affine, (g + a) / 2, np.linalg.norm(a - g) / 2 + r_eye)
        v = a - g
        t = ((xyz - g[:, None]) * v[:, None]).sum(0) / (v @ v)
        u = np.clip(t, 0, 1)
        inside = np.linalg.norm(xyz - (g[:, None] + u * v[:, None]), axis=0) <= r_eye + (r_apex - r_eye) * u
        inside &= -t * np.linalg.norm(v) <= GLOBE_FRONT + extra          # nothing in front of the globe: no eyelids
        keep[tuple(ijk[:, inside])] = True
    return keep


def lid_zone(shape, affine, eyes, apexes, extra=0.0):
    """In front of each globe (beyond GLOBE_FRONT, + extra when the eye was not found), within LID_RADIUS of the eye's
    axis: the eyelids and the skin around them. Removed whatever the face mask says."""
    zone = np.zeros(shape[:3], bool)
    for g, a in zip(eyes, apexes):
        front = (g - a) / np.linalg.norm(g - a)
        ijk, xyz = voxels_near(shape, affine, g + front * (GLOBE_FRONT + 30) / 2, (30 - GLOBE_FRONT) / 2 + LID_RADIUS + 10)
        d = xyz - g[:, None]
        along = front @ d
        across = np.linalg.norm(d - np.outer(front, along), axis=0)
        inside = (along > GLOBE_FRONT + extra) & (across <= LID_RADIUS)
        zone[tuple(ijk[:, inside])] = True
    return zone


def behind(shape, affine, eyes, apexes):
    """Voxels more than NECK_BEHIND mm behind the plane through both orbital apexes (the head's own front direction)."""
    front = (eyes - apexes).mean(0)
    front /= np.linalg.norm(front)
    xyz = affine[:3, :3] @ np.indices(shape[:3]).reshape(3, -1) + affine[:3, 3:4]
    return ((front @ (xyz - apexes.mean(0)[:, None])) < -NECK_BEHIND).reshape(shape[:3])


def brain_removed(crop, img, keep, tmp):
    """cm3 of brain (FSL bet2 on the reference image) that the keep mask would remove; None when bet2 is not available."""
    try:
        run("bet2", crop, tmp / "brain", "-m", "-n", "-f", "0.5")
        brain = np.asanyarray(nib.load(tmp / "brain_mask.nii.gz").dataobj) > 0
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return float((brain & ~keep).sum() * abs(np.linalg.det(img.affine[:3, :3])) / 1000)


def eye_kernels(mm):
    """An 8 mm ball (the vitreous) and a 13-17 mm ring (the tissue around the eye), on a grid of mm per voxel."""
    n = int(np.ceil(17 / mm))
    dist = np.sqrt((np.mgrid[-n:n + 1, -n:n + 1, -n:n + 1] ** 2).sum(0)) * mm
    ball, ring = (dist <= 8).astype(np.float32), ((dist >= 13) & (dist <= 17)).astype(np.float32)
    return ball / ball.sum(), ring / ring.sum()


def find_eyes(img, expected, sign):
    """Each eye near where it is expected: the centre of the darkest 8 mm ball ringed by brighter tissue, within
    EYE_SEARCH mm. Returns the centres (mm) and whether both look like eyes, about an eye distance apart."""
    data = np.asanyarray(img.dataobj).astype(np.float32)
    grid = np.mgrid[-32:33, -32:33, -32:33].astype(np.float32)
    dist = np.sqrt((grid ** 2).sum(0))
    ball, ring = eye_kernels(1)
    centres, good = [], True
    for e in expected:
        pts = nib.affines.apply_affine(np.linalg.inv(img.affine), grid.reshape(3, -1).T + e)
        cube = ndi.map_coordinates(data, pts.T, order=1, cval=0).reshape(dist.shape)
        cube /= max(np.percentile(cube, 95), 1e-6)
        score = sign * (fftconvolve(cube, ring, mode="same") - fftconvolve(cube, ball, mode="same"))
        score = np.where(dist <= EYE_SEARCH, score, -np.inf)
        i = np.unravel_index(np.argmax(score), score.shape)
        centres.append(e + np.array(i) - 32)
        good &= score[i] >= EYE_CONTRAST and dist[i] <= EYE_SEARCH - 2
    centres = np.array(centres)
    return centres, bool(good and 45 <= np.linalg.norm(centres[0] - centres[1]) <= 80)


def search_eyes(img, tissue, sign):
    """Both eyes found anywhere in the head, with no template: a pair of eye-like spots 50-78 mm apart, at about the same
    height and depth, in front of the centre of the head and 60-138 mm below its top. The maxillary sinuses look like
    eyes too (dark, ringed by tissue), so among the strong pairs the highest one is taken. None when there is no pair."""
    data = np.asanyarray(img.dataobj).astype(np.float32)
    zoom = np.array(img.header.get_zooms()[:3]) / 2.0                     # work at 2 mm
    small = ndi.zoom(data, zoom, order=1)
    small /= max(np.percentile(small, 95), 1e-6)
    ball, ring = eye_kernels(2)
    score = sign * (fftconvolve(small, ring, mode="same") - fftconvolve(small, ball, mode="same"))
    score[ndi.zoom(tissue.astype(np.uint8), zoom, order=0) == 0] = -np.inf
    peaks = np.argwhere((score == ndi.maximum_filter(score, size=9)) & (score >= EYE_CONTRAST))
    peaks = peaks[np.argsort(-score[tuple(peaks.T)])][:15]
    spots = nib.affines.apply_affine(img.affine, peaks / zoom)
    xyz = nib.affines.apply_affine(img.affine, np.argwhere(tissue))
    middle, top = xyz.mean(0), xyz[:, 2].max()
    pairs = []
    for i in range(len(spots)):
        for j in range(i + 1, len(spots)):
            d, height = np.abs(spots[i] - spots[j]), (spots[i][2] + spots[j][2]) / 2
            if (50 <= np.linalg.norm(d) <= 78 and d[1] < 20 and d[2] < 20 and min(spots[i][1], spots[j][1]) > middle[1]
                    and 60 <= top - height <= 138):
                pairs.append((score[tuple(peaks[i])] + score[tuple(peaks[j])], height, i, j))
    if not pairs:
        return None
    strongest = max(pairs)[0]
    _, _, i, j = max((p for p in pairs if p[0] >= 0.8 * strongest), key=lambda p: p[1])
    pair = spots[[i, j]]
    eyes, good = find_eyes(img, pair[np.argsort(pair[:, 0])], sign)     # the left eye first, as in EYES
    return eyes if good else None


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


def template_on(img, to_image):
    """pydeface's face mask, and the eyes and apexes of its template, carried onto an image by a matrix (mm to mm)."""
    m = np.linalg.inv(FACE.affine) @ np.linalg.inv(to_image) @ img.affine
    face = ndi.affine_transform(np.asanyarray(FACE.dataobj), m[:3, :3], m[:3, 3], output_shape=img.shape[:3], order=0, cval=1) > 0.5
    return face, nib.affines.apply_affine(to_image, EYES), nib.affines.apply_affine(to_image, APEXES)


def by_pydeface(crop, img, tissue, sign, tmp):
    """Placement 1: pydeface's own registration of its template (FSL FLIRT)."""
    run("pydeface", crop, "--outfile", tmp / "crop_defaced.nii.gz", "--force", "--nocleanup")
    scales = np.linalg.svd(np.loadtxt(tmp / "crop_pydeface.mat")[:3, :3], compute_uv=False)
    run("flirt", "-in", MARKS, "-ref", crop, "-applyxfm", "-init", tmp / "crop_pydeface.mat",
        "-interp", "nearestneighbour", "-out", tmp / "marks.nii.gz")
    marks = nib.load(tmp / "marks.nii.gz")
    m = np.asanyarray(marks.dataobj)
    if not (SCALES[0] <= scales.min() and scales.max() <= SCALES[1]) or not all((m == k).any() for k in (1, 2, 3, 4)):
        return None                                       # the template was stretched implausibly or fell outside the image
    points = np.array([nib.affines.apply_affine(marks.affine, np.argwhere(m == k).mean(0)) for k in (1, 2, 3, 4)])
    return np.asanyarray(nib.load(tmp / "crop_pydeface_mask.nii.gz").dataobj) > 0.5, points[:2], points[2:]


def by_eyes(crop, img, tissue, sign, tmp):
    """Placement 2: the template turned, sized and moved so that its eyes and head centre fall on the image's."""
    eyes = search_eyes(img, tissue, sign)
    if eyes is None:
        return None
    src = np.vstack([EYES, HEAD_CENTRE])
    dst = np.vstack([eyes, nib.affines.apply_affine(img.affine, np.argwhere(tissue).mean(0))])
    u, sv, vt = np.linalg.svd((dst - dst.mean(0)).T @ (src - src.mean(0)))
    d = np.diag([1, 1, np.sign(np.linalg.det(u @ vt))])
    m = np.eye(4)
    m[:3, :3] = (sv * d.diagonal()).sum() / ((src - src.mean(0)) ** 2).sum() * u @ d @ vt
    m[:3, 3] = dst.mean(0) - m[:3, :3] @ src.mean(0)
    return template_on(img, m)


def by_outline(crop, img, tissue, sign, tmp):
    """Placement 3, always possible: the box of the template's head stretched onto the box of this head."""
    xyz = nib.affines.apply_affine(img.affine, np.argwhere(tissue))
    m = np.eye(4)
    m[:3, :3] = np.diag((xyz.max(0) - xyz.min(0)) / (HEAD_BOX[1] - HEAD_BOX[0]))
    m[:3, 3] = xyz.min(0) - m[:3, :3] @ HEAD_BOX[0]
    return template_on(img, m)


PLACEMENTS = ((by_pydeface, "pydeface"), (by_eyes, "the eyes found in the image"), (by_outline, "the outline of the head"))


def face_and_neck(ref, tmp):
    """Keep mask on the reference image's own grid, how the face mask was placed, and whether a person should look."""
    native = nib.load(ref)
    sign = -1 if ref.name[:-7].endswith(BRIGHT_EYES) else 1
    up, crop = tmp / "headup.nii.gz", tmp / "crop.nii.gz"
    nib.as_closest_canonical(native).to_filename(up)      # FLIRT starts from the voxel axes: work head-up, like the template
    try:
        run("robustfov", "-i", up, "-r", crop)
    except subprocess.CalledProcessError:
        shutil.copyfile(up, crop)                         # no neck cut: the face mask also removes the front of the neck
    img = nib.load(crop)
    tissue = head(np.asanyarray(img.dataobj))
    tried = []
    for place, name in PLACEMENTS:
        try:
            placed = place(crop, img, tissue, sign, tmp)
        except subprocess.CalledProcessError:
            placed = None
        if placed is None:
            continue
        face, expected, apexes = placed
        eyes, found = find_eyes(img, expected, sign)
        if not found:
            eyes = expected
        apexes = apexes + eyes - expected
        orbits = orbit_cones(img.shape, img.affine, eyes, apexes, 0 if found else ORBIT_UNSURE)
        face = face & ~lid_zone(img.shape, img.affine, eyes, apexes, 0 if found else ORBIT_UNSURE)
        nose, ratio = cut_checks(img, tissue, face, orbits, eyes, apexes)
        passed = found and nose <= NOSE_LEFT and LOPSIDED[0] <= ratio <= LOPSIDED[1]
        tried.append((passed, found, -nose, name, face | orbits, eyes, apexes))
        if passed:
            break
    passed, _, _, name, keep, eyes, apexes = max(tried, key=lambda t: t[:3])
    note = None
    cut = brain_removed(crop, img, keep, tmp)
    if cut is not None and cut > BRAIN_CUT_CC:
        passed, note = False, f"{cut:.1f} cm3 of brain may have been removed: look at the check picture"
    whole = nib.load(up)
    inside = on_grid(np.ones(img.shape[:3], bool), img.affine, whole.shape, whole.affine)
    keep = on_grid(keep, img.affine, whole.shape, whole.affine)
    keep |= ~inside & behind(whole.shape, whole.affine, eyes, apexes)      # below the neck cut: keep the cord, not the jaw
    back = nib.orientations.ornt_transform(nib.io_orientation(whole.affine), nib.io_orientation(native.affine))
    keep = nib.orientations.apply_orientation(keep, back)
    nib.Nifti1Image(keep.astype(np.uint8), native.affine).to_filename(tmp / "keep_ref.nii.gz")
    return tmp / "keep_ref.nii.gz", name, (note or True) if not passed else False


def landmarks(out):
    """pydeface's template with the eye centres (1, 2) and orbital apexes (3, 4) marked as 4 mm balls."""
    marks = np.zeros(FACE.shape, np.uint8)
    for k, c in enumerate(list(EYES) + list(APEXES), 1):
        ijk, xyz = voxels_near(FACE.shape, FACE.affine, c, 4)
        inside = ((xyz - c[:, None]) ** 2).sum(0) <= 16
        marks[tuple(ijk[:, inside])] = k
    nib.Nifti1Image(marks, FACE.affine).to_filename(out)
    return out


def by_coordinates(keep, img, out):
    """A keep mask moved onto another image by the scanner coordinates alone; what lies outside the mask's image is removed."""
    k, target = nib.load(keep), nib.load(img)
    moved = on_grid(np.asanyarray(k.dataobj), k.affine, target.shape, target.affine)
    nib.Nifti1Image(moved.astype(np.uint8), target.affine).to_filename(out)
    return out


def reference(images):
    """The image the mask is made on: a 3D T1 without contrast if there is one, else another 3D head image, else the
    image with the most slices."""
    def rank(p):
        three_d = "/anat/" in p.as_posix() and sidecar(p)[1].get("MRAcquisitionType") == "3D"
        return (three_d and p.name.endswith("_T1w.nii.gz") and "ce-" not in p.name, three_d, "/anat/" in p.as_posix(),
                nib.load(p).shape[2])
    heads = [p for p in images if "part-phase" not in p.name and "acq-orbit" not in p.name and not is_held(p)
             and not is_spine(p)]
    return max(heads, key=rank) if heads else None


NOT_NEEDED = "not needed: spine image"


def is_spine(img):
    return "spine" in img.name or sidecar(img)[1].get("BRAIMSRegion") == "spine"


def is_held(img):
    return bool(sidecar(img)[1].get("BRAIMSHold"))


def hold(img, reason):
    side, meta = sidecar(img)
    meta["BRAIMSHold"] = reason
    side.write_text(json.dumps(meta, indent=4))


def original_of(img):
    """The image as converted, before defacing: kept at the site so that a mask can be applied again from scratch."""
    return saved / "orig" / img.relative_to(bids)


def first_volume(img, out):
    im = nib.load(img)
    data = np.asanyarray(im.dataobj)
    nib.Nifti1Image(data[..., 0] if data.ndim == 4 else data, im.affine).to_filename(out)
    return out


def saved_mask(img):
    return saved / img.relative_to(bids).parent / (img.name[:-7] + "_keepmask.nii.gz")


def apply_keep(img, keep, margin=0.0):
    """Zero what the keep mask removes, always starting from the converted image kept at the site. The mask is saved
    first, so a mask edited later is newer than the image (that is how --reapply finds it)."""
    orig = original_of(img)
    if not orig.exists():                                 # first defacing: keep the converted image at the site
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(img, orig)
    im = nib.load(orig)
    data = np.asanyarray(im.dataobj)
    km = nib.load(keep)
    k = np.asanyarray(km.dataobj) > 0.5
    target = saved_mask(img)
    if Path(keep) != target:                              # keep a copy of the mask for checking and editing
        target.parent.mkdir(parents=True, exist_ok=True)
        nib.Nifti1Image(k.astype(np.uint8), km.affine).to_filename(target)
    if margin:
        zooms = np.array(km.header.get_zooms()[:3], float)
        k = ndi.binary_dilation(k, ndi.generate_binary_structure(3, 1), iterations=max(1, int(round(margin / zooms.min()))))
    out = data * (k[..., None] if data.ndim == 4 else k)
    tmp = Path(str(img) + ".partial.nii.gz")
    nib.Nifti1Image(out.astype(data.dtype), im.affine, im.header).to_filename(tmp)
    tmp.replace(img)                                      # never a half-written image
    return 100 * (1 - k.mean())


import pydeface
FACE = nib.load(Path(pydeface.__file__).parent / "data" / "facemask.nii.gz")
MARKS = landmarks(Path(tempfile.mkdtemp()) / "orbit_landmarks.nii.gz")


def deface_session(session):
    """Deface one session; returns what it printed, the number of images defaced, those not defaced, and whether to look."""
    done, failed, look = 0, [], []
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        return _deface_session(session, done, failed, look, out)


def reapply_masks(session, images):
    """Images whose saved mask was edited after the image was defaced get that mask again, from the kept original."""
    done = 0
    for img in images:
        mask = saved_mask(img)
        if not mask.exists() or is_held(img):
            continue
        if sidecar(img)[1].get("Defaced") is True and mask.stat().st_mtime <= img.stat().st_mtime:
            continue                                      # mask not edited since this image was defaced
        removed = apply_keep(img, mask)
        side, meta = sidecar(img)
        meta.update({"Defaced": True, "NeckRemoved": True, "DefacingMethod": "saved keep mask (checked or edited by hand)",
                     "BRAIMSDefaceVersion": DEFACE_VERSION})
        meta.pop("DefacingNote", None)
        side.write_text(json.dumps(meta, indent=4))
        done += 1
        print(f"  done  {img.relative_to(session)}  (saved mask applied, {removed:.0f}% removed)")
    return done


def _deface_session(session, done, failed, look, out):
    images = [p for d in ("anat", "dwi") for p in sorted((session / d).glob("*.nii.gz")) if not p.name.endswith(".partial.nii.gz")]
    if reapply:
        print(session.relative_to(bids))
        done += reapply_masks(session, images)
    if all(sidecar(p)[1].get("Defaced") is True or is_held(p) or (is_spine(p) and sidecar(p)[1].get("DefacingMethod") == NOT_NEEDED)
           for p in images):
        return out.getvalue(), done, failed, look
    if not reapply:
        print(session.relative_to(bids))
    repair(session)
    images = [p for d in ("anat", "dwi") for p in sorted((session / d).glob("*.nii.gz")) if not p.name.endswith(".partial.nii.gz")]
    for img in images:                                    # spine images need no defacing (BRAIMS policy)
        if is_spine(img):
            side, meta = sidecar(img)
            if meta.get("BRAIMSHold", "").startswith("spine"):
                meta.pop("BRAIMSHold")
            meta.update({"BRAIMSRegion": "spine", "DefacingMethod": NOT_NEEDED})
            side.write_text(json.dumps(meta, indent=4))
    todo = [p for p in images if sidecar(p)[1].get("Defaced") is not True and not is_held(p) and not is_spine(p)]
    if not todo:
        return out.getvalue(), done, failed, look
    ref = reference(images)
    if ref is None:                                       # orbit-only session: no head image for the mask
        for img in todo:
            hold(img, "no head image in this session to place the face mask: kept at the site")
            print(f"  HELD  {img.relative_to(session)}  (no head image in this session)")
        return out.getvalue(), done, failed, look
    picture = saved / session.relative_to(bids) / f"{'_'.join(session.relative_to(bids).parts)}_deface_check.png"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. Neck (robustfov) and face (pydeface's mask, orbits kept) on the reference image -> a keep mask on its own grid
        source = original_of(ref) if original_of(ref).exists() else ref
        original = first_volume(source, tmp / ref.name)   # register from the untouched copy
        try:
            if sidecar(ref)[1].get("Defaced") is True:    # defaced in an earlier run: its saved mask is the session's
                keep_ref, placed, unsure = saved_mask(ref), None, bool(sidecar(ref)[1].get("DefacingNote"))
            else:
                keep_ref, placed, unsure = face_and_neck(original, tmp)
        except Exception as e:                            # an image that cannot be read at all
            print(f"  NOT DEFACED  the whole session: {ref.name} could not be read ({e})")
            failed += todo
            return out.getvalue(), done, failed, look
        if placed:
            print(f"  face mask placed by {placed}, on {ref.name}")
        if unsure:
            look.append(session)
            print(f"  LOOK  {unsure if isinstance(unsure, str) else 'no placement of the face mask passed the checks: the best one was used'}. Look at the check picture.")
        # 2. The same mask on every other image of the session
        masks = {}
        for img in todo:
            mag = Path(str(img).replace("part-phase", "part-mag"))
            how = f"robustfov + pydeface's face mask placed by {placed}, orbits kept"
            try:
                target = first_volume(original_of(img) if original_of(img).exists() else img,
                                      tmp / f"{img.name[:-7]}_vol0.nii.gz")
                keep = tmp / f"{img.name[:-7]}_keep.nii.gz"
                if img == ref:
                    keep = keep_ref
                elif "part-phase" in img.name and (mag in masks or saved_mask(mag).exists()):
                    keep = masks.get(mag, saved_mask(mag))      # the magnitude image of the same scan
                    how = f"the keep mask of {mag.name}"
                else:
                    how = f"the keep mask of {ref.name}, moved here with FSL FLIRT (6 dof)"
                    try:
                        if "part-phase" in img.name or min(nib.load(target).shape) < 5:
                            raise ValueError("too little anatomy to register")
                        # same session, same scanner coordinates: start from the header geometry, no wide rotation search
                        run("flirt", "-in", original, "-ref", target, "-dof", "6", "-cost", "mutualinfo", "-usesqform",
                            "-nosearch", "-omat", tmp / "move.mat")
                        run("flirt", "-in", keep_ref, "-ref", target, "-applyxfm", "-init", tmp / "move.mat",
                            "-interp", "nearestneighbour", "-out", keep)
                    except (ValueError, subprocess.CalledProcessError):
                        by_coordinates(keep_ref, target, keep)
                        how = f"the keep mask of {ref.name}, moved here by the scanner coordinates"
                masks[img] = keep
                removed = apply_keep(img, keep, EPI_MARGIN if "/dwi/" in img.as_posix() else 0.0)
            except Exception as e:                        # an image that cannot be read at all
                print(f"  NOT DEFACED  {img.relative_to(session)}  ({e})")
                failed.append(img)
                continue
            if img == ref and qc:
                qc.before_after(original, img, picture)
                print(f"  check picture: {picture.relative_to(bids)}")
            side, meta = sidecar(img)
            meta.update({"Defaced": True, "NeckRemoved": False, "DefacingMethod": how + "; neck kept behind the face",
                         "BRAIMSDefaceVersion": DEFACE_VERSION})
            if unsure:
                meta["DefacingNote"] = unsure if isinstance(unsure, str) else "no placement of the face mask passed the checks: look at the check picture"
            side.write_text(json.dumps(meta, indent=4))
            done += 1
            print(f"  done  {img.relative_to(session)}  ({removed:.0f}% of the volume removed"
                  + (", mask moved by the scanner coordinates)" if how.endswith("scanner coordinates") else ")"))
    return out.getvalue(), done, failed, look


def workers_for(n_sessions):
    """One session per processor (FLIRT uses one each), but no more than the memory allows (~2 GB per session)."""
    cpus = os.cpu_count() or 1
    try:
        with open("/proc/meminfo") as h:
            avail = next(int(line.split()[1]) for line in h if line.startswith("MemAvailable")) / 1024 ** 2
    except (OSError, StopIteration, ValueError):
        avail = 2.0 * cpus
    return max(1, min(n_sessions, cpus, int(avail // 2)))


def main():
    done, failed, look = 0, [], []
    sessions = sorted({p.parent.parent for d in ("anat", "dwi") for p in bids.glob(f"sub-*/**/{d}/*.nii.gz")
                       if "derivatives" not in p.relative_to(bids).parts})
    results, left = [], list(sessions)
    try:
        with ProcessPoolExecutor(workers_for(len(sessions))) as pool:
            for session, result in zip(sessions, pool.map(deface_session, sessions)):
                results.append(result)
                left.remove(session)
    except BrokenProcessPool:                             # a worker was killed (usually: out of memory)
        print(f"Out of memory or a crash with several sessions at once: {len(left)} session(s) again, one at a time.")
    for session in left:
        results.append(deface_session(session))
    for text, n, bad, unsure in results:
        print(text, end="", flush=True)
        done, failed, look = done + n, failed + bad, look + unsure
    print(f"\n{done} image(s) defaced. Look at one image per session before you go on.")
    if look:
        print(f"{len(look)} session(s) need a look at their check picture first: " + ", ".join(str(s.relative_to(bids)) for s in look))
    if failed:
        print(f"{len(failed)} image(s) could not be read. They stay at the site: the BRAIMS check will not package them.")


if __name__ == "__main__":
    main()
