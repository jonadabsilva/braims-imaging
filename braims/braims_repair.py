"""Repairs made on each session before defacing, so that every image can be defaced and sent (called by braims_deface.py).

even copies   Slices are missing from the export: the converter stores the image with a wrong slice spacing and adds an
              evenly resampled copy ("Eq"). The Eq copy takes the place of the stretched image.
extra copies  The exam folder held a series twice (names ending in an extra letter, or in reala, imaginarya, pha or Tilt).
              The gantry-tilt-corrected or the largest image keeps the series' own name; the others stay as rec-copy2, ...
headers       The two orientation matrices of an image disagree: the one that fits the voxel size is kept.
mosaics       A scanner mosaic that the converter could not unpack (the export lost the scanner's private header) is
              unpacked: tiles in reading order, positions from the header, stacking direction checked on another image.
Every repair is printed and written in the image's sidecar ("ImageRepair").
"""
import json, re
from pathlib import Path
import numpy as np
import nibabel as nib
from scipy import ndimage as ndi
from braims_check import SUFFIXES

EXTENSIONS = (".nii.gz", ".json", ".bval", ".bvec")
PART = {"reala": "part-real", "imaginarya": "part-imag", "pha": "part-phase"}
AFTER_REC = ("dir-", "run-", "echo-", "part-")           # BIDS puts rec- before these entities


def head(data):
    """Head mask: smoothed image above a third of the Otsu threshold, largest piece, holes filled."""
    sm = ndi.gaussian_filter(data.astype(np.float32), 1.2)
    hist, edges = np.histogram(sm[sm > 0], 256)
    c = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    m0, m1 = np.cumsum(hist * c) / np.maximum(w0, 1), (np.sum(hist * c) - np.cumsum(hist * c)) / np.maximum(w0[-1] - w0, 1)
    lab, n = ndi.label(sm > 0.35 * c[np.argmax(w0 * (w0[-1] - w0) * (m0 - m1) ** 2)])
    return ndi.binary_fill_holes(lab == np.argmax(np.bincount(lab.ravel())[1:]) + 1) if n else lab > 0


def on_grid(mask, mask_affine, shape, affine, outside=0):
    """A mask moved onto another grid by the scanner coordinates alone (nearest voxel)."""
    m = np.linalg.inv(mask_affine) @ affine
    return ndi.affine_transform(mask.astype(np.uint8), m[:3, :3], m[:3, 3], output_shape=tuple(shape[:3]), order=0, cval=outside) > 0


def volume(img):
    """First volume of an image, as an array."""
    data = np.asanyarray(nib.load(img).dataobj)
    return data[..., 0] if data.ndim == 4 else data


def rename(folder, old, new):
    for ext in EXTENSIONS:
        if (folder / (old + ext)).exists():
            (folder / (old + ext)).replace(folder / (new + ext))


def note(img, text):
    side = Path(str(img)[:-7] + ".json")
    meta = json.loads(side.read_text()) if side.exists() else {}
    meta["ImageRepair"] = "; ".join(filter(None, [meta.get("ImageRepair"), text]))
    side.write_text(json.dumps(meta, indent=4))
    print(f"  repaired  {img.parent.name}/{img.name}  ({text})")


def even_copies(session):
    for src in sorted(session.glob("*/*.nii.gz")):
        name = re.sub(r"Eq\d+(?=_)", "", src.name)
        if name != src.name:
            src.replace(src.with_name(name))             # the Eq copy has no sidecar: the stretched image's own stays
            note(src.with_name(name), "slices were missing from the export: the converter's evenly resampled copy is used")


def own_name(name):
    """The name a converter copy should have had, or None when the name is not a copy's."""
    parts = name.split("_")
    was = list(parts)
    if parts[-1] not in SUFFIXES and parts[-1][:-1] in SUFFIXES:
        parts[-1] = parts[-1][:-1]
    for i, p in enumerate(parts[:-1]):
        m = re.fullmatch(r"(acq-.*?)(reala|imaginarya|pha|Tilt\d*)", p)
        if m:
            parts[i] = m.group(1)
            if m.group(2) in PART and not any(q.startswith("part-") for q in parts):
                parts.insert(len(parts) - 1, PART[m.group(2)])
    parts = [p for p in parts if p != "acq-"]
    return "_".join(parts) if parts != was else None


def extra_copies(session):
    groups = {}
    for img in sorted(session.glob("*/*.nii.gz")):
        own = own_name(img.name[:-7])
        if own and (img.parent / (own + ".nii.gz")).exists():
            groups.setdefault(img.parent / own, [img.parent / (own + ".nii.gz")]).append(img)
    for own, images in groups.items():
        ranked = sorted(images, key=lambda p: ("Tilt" in p.name, np.prod(nib.load(p).shape)), reverse=True)
        names = [own.name]
        for k in range(2, len(ranked) + 1):
            parts = own.name.split("_")
            at = next((i for i, p in enumerate(parts) if p.startswith(AFTER_REC)), len(parts) - 1)
            names.append("_".join(parts[:at] + [f"rec-copy{k}"] + parts[at:]))
        for k, img in enumerate(ranked):                 # through temporary names: the copies swap places
            rename(own.parent, img.name[:-7], f"renaming{k}")
        for k, name in enumerate(names):
            rename(own.parent, f"renaming{k}", name)
            note(own.parent / (name + ".nii.gz"), "the export held this series more than once: " +
                 ("this is the largest image" if k == 0 else "this is another copy, kept for the core center"))


def fix_header(img):
    h = nib.load(img).header
    if not (h["sform_code"] and h["qform_code"]):
        return
    s, q = h.get_sform(), h.get_qform()
    if np.abs(np.linalg.norm(s[:3, :3], axis=0) / np.array(h.get_zooms()[:3]) - 1).max() > 0.1 and not np.allclose(s, q, atol=0.01):
        im = nib.load(img)
        data = np.asanyarray(im.dataobj)
        im.header.set_sform(q, int(h["qform_code"]))
        nib.Nifti1Image(data, None, im.header).to_filename(img)
        note(img, "the image's two orientation matrices disagreed: the one that fits the voxel size is kept")


def unmosaic(img, like):
    side = Path(str(img)[:-7] + ".json")
    meta = json.loads(side.read_text()) if side.exists() else {}
    im = nib.load(img)
    tile = meta.get("AcquisitionMatrixPE")
    if len(im.shape) < 3 or im.shape[2] != 1 or "MOSAIC" not in meta.get("ImageType", []) or not tile or im.shape[1] % tile:
        return
    flat = np.asanyarray(im.dataobj).reshape(im.shape[0], im.shape[1], -1)
    n = im.shape[1] // tile                              # tiles per row; an export may pad the columns at the end
    tiles = [flat[c * tile:(c + 1) * tile, (n - 1 - r) * tile:(n - r) * tile] for r in range(n) for c in range(n)]
    count = max(k for k, t in enumerate(tiles) if t.any()) + 1
    data = np.stack(tiles[:count], axis=2)
    affine = im.affine.copy()                            # the header places the mosaic as if its centre were the slice's
    affine[:3, 3] += im.affine[:3, :3] @ [(n - 1) * tile / 2, (n - 1) * tile / 2, 0]
    if like:                                             # slices stacked along the header's normal, or against it?
        ref, mine, overlap = nib.load(like), head(data[..., 0]), {}
        tissue = head(volume(like))
        for sign in (1, -1):
            a = affine.copy()
            a[:3, 2] *= sign
            overlap[sign] = (on_grid(tissue, ref.affine, mine.shape, a) & mine).sum()
        affine[:3, 2] *= max(overlap, key=overlap.get)
    out = nib.Nifti1Image(data if len(im.shape) > 3 else data[..., 0], affine)
    out.header.set_zooms(im.header.get_zooms()[:out.ndim])
    out.header.set_xyzt_units(*im.header.get_xyzt_units())
    out.to_filename(img)
    note(img, f"scanner mosaic unpacked into {count} slices: the export had lost the scanner's private header")


def repair(session):
    even_copies(session)
    extra_copies(session)
    images = sorted(session.glob("*/*.nii.gz"))
    whole = [p for p in images if len(nib.load(p).shape) > 2 and nib.load(p).shape[2] > 1]
    like = max(whole, key=lambda p: np.prod(nib.load(p).shape[:3])) if whole else None
    for img in images:
        fix_header(img)
    for img in images:
        unmosaic(img, like)
    for tsv in session.glob("*_scans.tsv"):
        lines = tsv.read_text().splitlines()
        rows = {line.split("\t")[0]: line for line in lines[1:]}
        have = sorted(f"{p.parent.name}/{p.name}" for p in session.glob("*/*.nii.gz"))
        new = [rows.get(f, "\t".join([f] + ["n/a"] * lines[0].count("\t"))) for f in have]
        if new != lines[1:]:
            tsv.write_text("\n".join([lines[0]] + new) + "\n")
