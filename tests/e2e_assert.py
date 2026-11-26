"""Assertions on a BRAIMS folder after `run` and `package` on the synthetic exams of make_exams.py (real tools in Docker).

    python tests/e2e_assert.py <braims_folder>

Prints PASS/FAIL per scenario; exit code 1 if anything failed.
"""
import json, re, sys, zipfile
from pathlib import Path
import numpy as np
import nibabel as nib

EYES = np.array([(-32.0, 58.0, -32.0), (32.0, 58.0, -32.0)])        # globe centres in the MNI heads (world mm, RAS)
NOSE = np.array((-4.0, 90.0, -55.0))
SECRETS = [b"SILVA", b"JOAO", b"HC1234567", b"19700315", b"Hospital Teste", b"Rua A 1", b"HOUSE", b"TECH^ANA",
           b"MRC40123", b"40123", b"ACC998877", b"OUTRO", b"OTHER999", b"20190614", b"20200901", b"2019-06-14"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


def sample(img, centre, radius):
    """Voxel values of an image within radius mm of a world point."""
    data = np.asanyarray(img.dataobj)
    if data.ndim == 4:
        data = data[..., 0]
    inv = np.linalg.inv(img.affine)
    g = np.mgrid[-radius:radius + 1, -radius:radius + 1, -radius:radius + 1].reshape(3, -1).T
    g = g[np.linalg.norm(g, axis=1) <= radius] + centre
    v = np.round(nib.affines.apply_affine(inv, g)).astype(int)
    inside = np.all((v >= 0) & (v < data.shape[:3]), axis=1)
    return data[tuple(v[inside].T)]


def meta(p):
    return json.loads(p.with_name(p.name[:-7] + ".json").read_text())


def main():
    folder = Path(sys.argv[1])
    bids = folder / "bids"
    anat = lambda sub, ses="ses-01": sorted((bids / sub / ses / "anat").glob("*.nii.gz"))

    # naming
    names = {p.name for p in anat("sub-BR010001")}
    check("FLAIR (dark-fluid) named FLAIR", any(n.endswith("acq-2D_FLAIR.nii.gz") for n in names), names)
    check("orbit STIR series kept and named", sum("acq-orbitSTIR" in n for n in names) >= 1, names)
    check("MPRAGE named", any("acq-MPRAGE_T1w" in n for n in names), names)

    # defacing of the adult head
    t1 = next(p for p in anat("sub-BR010001") if "MPRAGE_T1w" in p.name)
    im = nib.load(t1)
    check("adult: nose removed", np.mean(sample(im, NOSE, 5) > 0) < 0.05)
    for e in EYES:
        check(f"adult: eye at x={e[0]:+.0f} kept", np.mean(sample(im, e, 6) > 0) > 0.95)
        check(f"adult: optic nerve behind eye x={e[0]:+.0f} kept", np.mean(sample(im, e + (-np.sign(e[0]) * 8, -25, 0), 3) > 0) > 0.95)
        orig = nib.load(bids / "derivatives" / "braims_deface" / "orig" / t1.relative_to(bids))
        before = np.sum(sample(orig, e + (0, 19, 0), 4) > 0)
        after = np.sum(sample(im, e + (0, 19, 0), 4) > 0)
        check(f"adult: eyelid in front of eye x={e[0]:+.0f} removed", before == 0 or after <= 0.1 * before, f"{after}/{before}")
    for label, pt in (("frontal pole", (-15, 55, 0)), ("frontal pole", (15, 55, 0)), ("orbitofrontal cortex", (-10, 40, -20)),
                      ("orbitofrontal cortex", (10, 40, -20)), ("forehead brain above the eyes", (-30, 45, 5))):
        check(f"adult: {label} at {pt} kept", np.mean(sample(im, np.array(pt, float), 3) > 0) == 1.0)
    check("adult: defaced sidecar", meta(t1).get("Defaced") is True and meta(t1).get("BRAIMSDefaceVersion"))
    for p in anat("sub-BR010001"):
        if "orbit" in p.name:
            o = nib.load(p)
            for e in EYES:
                check(f"orbit image {p.name[-30:]}: eye x={e[0]:+.0f} kept", np.mean(sample(o, e, 5) > 0) > 0.9)

    # child, tilted head
    for sub, label in (("sub-BR010003", "child"), ("sub-BR010002", "tilted GE head")):
        imgs = anat(sub)
        if not (bids / sub).exists():
            continue
        check(f"{label}: all images defaced", imgs and all(meta(p).get("Defaced") for p in imgs))
        look = [p for p in imgs if meta(p).get("DefacingNote")]
        print(f"      {label}: {len(look)} image(s) flagged for a look")

    # holds
    spine = [p for p in anat("sub-BR010004") if "spine" in p.name]
    check("spine: both series converted and named", len(spine) == 2, [p.name for p in anat("sub-BR010004")])
    check("spine: held, not defaced with the brain mask", spine and all(meta(p).get("BRAIMSHold") and not meta(p).get("Defaced") for p in spine))
    brain4 = [p for p in anat("sub-BR010004") if "spine" not in p.name]
    check("spine session: brain images defaced", brain4 and all(meta(p).get("Defaced") for p in brain4))
    orbit_only = anat("sub-BR010005")
    check("orbit-only: both series converted", len(orbit_only) == 2, [p.name for p in orbit_only])
    check("orbit-only: held", orbit_only and all(meta(p).get("BRAIMSHold") for p in orbit_only))

    # analysis information
    s = (bids / "sub-BR010001" / "sub-BR010001_sessions.tsv").read_text().split()
    check("days_from_baseline", s[-4:] == ["ses-01", "0", "ses-02", "445"], s)
    check("scanner ID", all("BRAIMSScannerID" in meta(p) for p in anat("sub-BR010001")))

    # package
    zips = sorted((folder / "outbox").glob("*.zip"))
    check("package made", zips)
    if zips:
        with zipfile.ZipFile(zips[-1]) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names if not n.endswith(".nii.gz"))
            heads = b"".join(z.read(n)[:2000] for n in names if n.endswith(".nii.gz"))
        leaks = [s for s in SECRETS if s in blob]
        check("package: no identifier in sidecars or tables", not leaks, leaks)
        check("package: no text in image headers", not re.search(rb"SILVA|JOAO|Hospital", heads))
        check("package: no held image", not any("spine" in n or "BR010005" in n for n in names))
        check("package: no derivatives, code or originals", not any(n.split("/")[0] in ("derivatives", "code") for n in names))
        side = json.loads(zipfile.ZipFile(zips[-1]).read(next(n for n in names if n.endswith("MPRAGE_T1w.json"))))
        check("package: acquisition parameters kept", all(k in side for k in ("EchoTime", "RepetitionTime", "Manufacturer",
                                                                              "MagneticFieldStrength", "BRAIMSScannerID")), sorted(side))
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
