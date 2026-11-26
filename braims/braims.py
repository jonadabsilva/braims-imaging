#!/usr/bin/env python3
"""BRAIMS site pipeline. Runs inside the BRAIMS Docker image; /data is the site's BRAIMS folder.

    braims run       sort, name, convert, remove neck and face, check and fix, then write the report
    braims package   build the upload package (refused while the check finds errors)
    braims reapply   after correcting masks in ITK-SNAP: apply them again (from the kept originals), check, report
    braims deface    step-by-step route: remove the neck and face in bids/ (add --reapply after editing masks)

The site's BRAIMS folder holds:
    raw/sub-<code>/ses-<nn>/...   the DICOM exams, one folder per coded exam (never leaves the site)
    bids/                         the converted data (made here)
    reports/                      one report per run (stays at the site: it shows faces before defacing)
    outbox/                       the upload packages
    naming_rules_local.csv        optional: extra naming rules sent by the core center
"""
import base64, datetime, html, json, re, subprocess, sys
from pathlib import Path
import yaml
import braims_naming as naming
import braims_meta as meta

DATA = Path("/data")
RAW, BIDS, REPORTS, OUTBOX = DATA / "raw", DATA / "bids", DATA / "reports", DATA / "outbox"
HERE = Path(__file__).resolve().parent
BIDSMAP = BIDS / "code" / "bidscoin" / "bidsmap.yaml"


def title(text):
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}", flush=True)


def sh(*cmd, capture=False):
    print("$ " + " ".join(map(str, cmd)), flush=True)
    if capture:
        out = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
        print(out.stdout, flush=True)
        return out.returncode, out.stdout
    return subprocess.run([str(c) for c in cmd]).returncode, ""


def rules():
    return naming.rules(DATA / "naming_rules_local.csv", HERE / "naming_rules.csv")          # local rules win


def name_series():
    """Apply the BRAIMS naming rules to every series type in the study bidsmap; return (series, result, note) rows."""
    table, rows = rules(), []
    bm = yaml.safe_load(BIDSMAP.read_text(encoding="utf-8"))
    dcm2niix = bm.setdefault("Options", {}).setdefault("plugins", {}).setdefault("dcm2niix2bids", {})
    dcm2niix["anon"] = "y"                              # round ages, drop acquisition dates (BIDScoin)
    dicom = bm["DICOM"]
    for key in ("height", "weight"):
        (dicom.get("participant") or {}).pop(key, None)
    for run in dicom.get("exclude") or []:
        rows.append((run["attributes"].get("SeriesDescription", ""), "excluded", "derived image (BIDScoin)"))
    for dt in [k for k in dicom if k not in ("participant", "exclude")]:
        for run in list(dicom[dt] or []):
            a = run["attributes"]
            desc = str(a.get("SeriesDescription") or a.get("ProtocolName") or "")
            new, note = naming.name_run(run, dt, table, naming.dicom_hint(run.get("provenance", "")))
            if new != dt:
                dicom[dt].remove(run)
                dicom[new] = (dicom.get(new) or []) + [run]
            rows.append((desc, naming.result_name(new, run), note))
    BIDSMAP.write_text(yaml.safe_dump(bm, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return rows


def picture(path):
    return f'<img src="data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}" style="width:520px">'


def report(rows, deface_out, check_out, check_ok):
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    out = REPORTS / f"BRAIMS_report_{stamp}.html"
    esc = html.escape
    flag = lambda note: ' style="color:#C00000;font-weight:bold"' if note.startswith(("NOT", "no BRAIMS")) or "POSSIBLY" in note or note.startswith("spine") else ""
    series = "".join(f"<tr><td>{esc(d)}</td><td>{esc(r)}</td><td{flag(n)}>{esc(n)}</td></tr>" for d, r, n in sorted(set(rows)))
    sessions = []
    for ses in sorted({p.parent.parent for p in BIDS.glob("sub-*/**/*.nii.gz")}):
        files = "<br>".join(esc(str(p.relative_to(ses))) for p in sorted(ses.glob("*/*.nii.gz")))
        pics = sorted((BIDS / "derivatives" / "braims_deface" / ses.relative_to(BIDS)).glob("*_deface_check.png"))
        sessions.append(f"<h3>{esc(str(ses.relative_to(BIDS)))}</h3><p>{files}</p>" +
                        "".join(f"<p><b>Top: before. Bottom: after neck and face removal.</b><br>{picture(p)}</p>" for p in pics))
    held = []
    for side in sorted(BIDS.glob("sub-*/ses-*/*/*.json")):
        try:
            why = json.loads(side.read_text(encoding="utf-8")).get("BRAIMSHold")
        except ValueError:
            why = None
        if why:
            held.append(f"<tr><td>{esc(str(side.relative_to(BIDS))[:-5])}</td><td>{esc(why)}</td></tr>")
    held_html = ("<table><tr><th>Image</th><th>Why it stays at the site</th></tr>" + "".join(held) + "</table>"
                 if held else "<p>None.</p>")
    trouble = "".join(f"<li><b>{esc(w)}</b>: {esc(m)}</li>" for w, m in problems)
    verdict = ('<p style="color:#00703C;font-weight:bold">The check found no errors. A second person reads this report, '
               'then the site runs Package.</p>' if check_ok else
               '<p style="color:#C00000;font-weight:bold">The check found errors (section 4). Do not send. Fix them as Part E of '
               'the manual says, then run again, or contact the core center.</p>')
    out.write_text(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>BRAIMS report {stamp}</title>
<style>body{{font-family:Arial,sans-serif;max-width:1000px;margin:24px auto;color:#222}} h1,h2{{color:#1F4E79}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #bbb;padding:4px 8px;font-size:14px;vertical-align:top}}
th{{background:#1F4E79;color:#fff;text-align:left}} pre{{background:#f4f4f4;padding:10px;font-size:12px;white-space:pre-wrap}}</style></head><body>
<h1>BRAIMS report</h1><p>{stamp.replace('_', ' ')}. This report shows faces before defacing: it stays at the site.</p>{verdict}
{f'<h2>Problems during the run</h2><ul style="color:#C00000">{trouble}</ul>' if trouble else ''}
<h2>1. How each series was named</h2><table><tr><th>Series on the scanner</th><th>BRAIMS name</th><th>Why</th></tr>{series}</table>
<h2>2. Each session, with the defacing check</h2>{''.join(sessions)}
<h2>3. Neck and face removal</h2><pre>{esc(deface_out)}</pre>
<h2>4. Check</h2><pre>{esc(check_out)}</pre>
<h2>5. Images held at the site (not in the package)</h2>{held_html}</body></html>""", encoding="utf-8")
    return out


class Lock:
    """One run at a time on a BRAIMS folder (two double-clicks would write the same files)."""
    path = DATA / ".braims_running"

    def __enter__(self):
        if self.path.exists() and datetime.datetime.now().timestamp() - self.path.stat().st_mtime < 24 * 3600:
            sys.exit(f"BRAIMS is already running on this folder (started {self.path.read_text().strip()}). Wait for it to "
                     f"finish. If no BRAIMS window is open, delete the file {self.path.name} in the BRAIMS folder.")
        self.path.write_text(datetime.datetime.now().isoformat(timespec="minutes"))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def step(name, *cmd, capture=False):
    """Run one tool; a failure is printed and noted for the report, never hidden."""
    code, out = sh(*cmd, capture=capture)
    if code:
        problems.append((name, f"ended with an error (code {code}); see the terminal output"))
        print(f"\n!! {name} ended with an error (code {code}). The check will block anything incomplete.\n", flush=True)
    return code, out


problems = []


def run():
    with Lock():
        title("0/7 Check the exam folders before starting")
        found = meta.preflight(RAW, DATA)
        if found:
            for where, what in found:
                print(f"  STOP  {where}: {what}")
            sys.exit("\nNothing was run. Fix the folders above, then run again.")
        print("  The exam folders are fine.")
        for ses in meta.stale_sessions(RAW, BIDS):          # interrupted before, or the exam changed: convert again
            print(f"  {ses.relative_to(BIDS)}: converted again (interrupted before, or the exam folder changed)")
            meta.forget(ses, BIDS)
        title("1/7 Sort the series (dicomsort)")
        step("dicomsort", "dicomsort", RAW, "-i", "sub-", "-j", "ses-", "-p", ".*")
        title("2/7 Scan the sequences (bidsmapper)")
        step("bidsmapper", "bidsmapper", RAW, BIDS, "-a")
        title("3/7 Name the sequences (BRAIMS naming rules)")
        rows = name_series()
        for d, r, n in sorted(set(rows)):
            print(f"  {d[:40]:40}  {r[:45]:45}  {n}")
        title("4/7 Convert (bidscoiner)")
        code, _ = step("bidscoiner", "bidscoiner", RAW, BIDS)
        if not code:
            meta.mark_converted(RAW, BIDS)
        finish(rows)


def finish(rows, deface_args=()):
    title("5/7 Remove the neck and face")
    _, deface_out = step("defacing", "python", HERE / "braims_deface.py", BIDS, *deface_args, capture=True)
    title("6/7 Keep what analysis needs: scanner IDs, days between sessions")
    print(f"  scanner IDs written in {meta.scanner_ids(BIDS)} sidecar(s)")
    print(f"  sessions.tsv written for {len(meta.write_sessions(BIDS, RAW))} subject(s)")
    meta.provenance(BIDS)
    title("7/7 Check and fix")
    sh("python", HERE / "braims_check.py", BIDS, "--fix")
    code, check_out = sh("python", HERE / "braims_check.py", BIDS, capture=True)
    out = report(rows, deface_out, check_out, code == 0 and not problems)
    title("Done")
    print(f"Open the report: reports/{out.name}\nA second person reads it. If it is right, run Package.")


def reapply():
    """After correcting masks in ITK-SNAP: apply the saved masks again (from the kept originals), check and report."""
    with Lock():
        finish([], ("--reapply",))


def package():
    OUTBOX.mkdir(exist_ok=True)
    subs = sorted(p.name for p in BIDS.glob("sub-*"))
    site = subs[0][4:8] if subs else "site"
    target = OUTBOX / f"BRAIMS_{site}_{datetime.date.today():%Y-%m-%d}.zip"
    code, _ = sh("python", HERE / "braims_check.py", BIDS, "--package", target)
    if code == 0:
        print(f"\nSend {target.name} and {target.name}.sha256 from the outbox folder.")
    sys.exit(code)


def deface():
    sys.exit(subprocess.run(["python", str(HERE / "braims_deface.py"), str(BIDS), *sys.argv[2:]]).returncode)


if __name__ == "__main__":
    {"run": run, "package": package, "deface": deface, "reapply": reapply}.get(sys.argv[1] if len(sys.argv) > 1 else "run",
                                                         lambda: sys.exit(__doc__))()
