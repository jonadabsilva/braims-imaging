"""Synthetic DICOM exams for testing the BRAIMS pipeline end to end.

A series is sampled from a NIfTI head (with a face) on any plane, slice thickness and field of view, and written as a
classic one-file-per-slice MR DICOM series, with the identifying attributes the pipeline must remove.

    write_series(volume_nifti, out_dir, description="t1_mprage_sag", plane="sag", ...)
"""
import datetime
from pathlib import Path
import numpy as np
import nibabel as nib
from scipy import ndimage as ndi
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

# Row, column and slice-normal direction cosines in DICOM patient space (LPS)
PLANES = {
    "ax": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),      # rows go right->left, columns anterior->posterior, slices head->foot
    "sag": ((0, 1, 0), (0, 0, -1), (1, 0, 0)),     # sagittal, slices right->left
    "cor": ((1, 0, 0), (0, 0, -1), (0, 1, 0)),     # coronal, slices anterior->posterior
}

PATIENT = dict(PatientName="SILVA^JOAO", PatientID="HC1234567", PatientBirthDate="19700315", PatientSex="M",
               PatientAge="049Y", PatientWeight="70", InstitutionName="Hospital Teste", InstitutionAddress="Rua A 1",
               ReferringPhysicianName="DR^HOUSE", OperatorsName="TECH^ANA", StationName="MRC40123",
               DeviceSerialNumber="40123", AccessionNumber="ACC998877")


def _tilt(rows, cols, degrees):
    """Rotate the row/column directions about the column direction (a gantry-tilt-like oblique)."""
    r, c = np.array(rows, float), np.array(cols, float)
    n = np.cross(r, c)
    t = np.radians(degrees)
    r2 = r * np.cos(t) + n * np.sin(t)
    return tuple(r2), tuple(c)


def write_series(volume, out_dir, description, plane="sag", spacing=1.0, thickness=None, gap=0.0, fov=None,
                 matrix=None, center=None, tilt=0.0, series_number=1, study=None, patient=None, extra=None,
                 mr_type=None, image_type=("ORIGINAL", "PRIMARY", "M", "ND"), drop_slices=(), intensity=1.0):
    """Write one series. volume: NIfTI path or image (RAS). fov: (rows_mm, cols_mm, slab_mm). center: LPS mm.
    drop_slices: slice indices left out of the export (to simulate an incomplete export)."""
    img = nib.load(str(volume)) if not isinstance(volume, nib.Nifti1Image) else volume
    data = np.asanyarray(img.dataobj).astype(np.float32)
    inv = np.linalg.inv(img.affine)
    rows, cols, normal = (np.array(v, float) for v in PLANES[plane])
    if tilt:
        rows, cols = (np.array(v) for v in _tilt(rows, cols, tilt))
        normal = np.cross(rows, cols)
    thickness = thickness or spacing
    step = thickness + gap
    if center is None:
        ras_c = nib.affines.apply_affine(img.affine, (np.array(data.shape) - 1) / 2)
        center = ras_c * [-1, -1, 1]
    fov = fov or (230.0, 230.0, 180.0)
    n_r = matrix[0] if matrix else int(round(fov[0] / spacing))
    n_c = matrix[1] if matrix else int(round(fov[1] / spacing))
    px = (fov[0] / n_r, fov[1] / n_c)
    n_s = max(1, int(round(fov[2] / step)))
    study = study or {}
    patient = {**PATIENT, **(patient or {})}
    study_uid = study.get("uid") or generate_uid()
    for_uid = study.get("for_uid") or generate_uid()
    series_uid = generate_uid()
    date = study.get("date", "20190614")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    first = np.array(center) - rows * px[1] * (n_c - 1) / 2 - cols * px[0] * (n_r - 1) / 2 - normal * step * (n_s - 1) / 2
    ii, jj = np.meshgrid(np.arange(n_r), np.arange(n_c), indexing="ij")
    written = 0
    for k in range(n_s):
        if k in drop_slices:
            continue
        origin = first + normal * step * k
        # average across the slice thickness (thick 2D slices look like real 2D slices)
        sub = np.linspace(-thickness / 2, thickness / 2, max(1, int(round(thickness))) + 1)[:-1] + thickness / (2 * max(1, int(round(thickness))))
        acc = np.zeros((n_r, n_c), np.float32)
        for s in sub:
            lps = origin[None, None, :] + rows * (jj[..., None] * px[1]) + cols * (ii[..., None] * px[0]) + normal * s
            ras = lps * [-1, -1, 1]
            vox = nib.affines.apply_affine(inv, ras.reshape(-1, 3)).T
            acc += ndi.map_coordinates(data, vox, order=1, cval=0).reshape(n_r, n_c)
        pix = np.clip(acc / len(sub) * intensity, 0, 4095).astype(np.uint16)
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = MRImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
        ds.Modality, ds.Manufacturer = "MR", study.get("manufacturer", "SIEMENS")
        ds.ManufacturerModelName = study.get("model", "Skyra")
        ds.MagneticFieldStrength = study.get("field", 3)
        ds.SoftwareVersions = study.get("software", "syngo MR E11")
        for k2, v in patient.items():
            setattr(ds, k2, v)
        ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = date
        ds.StudyTime = ds.SeriesTime = ds.AcquisitionTime = "101010.000000"
        ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
        ds.StudyID, ds.StudyDescription = "1", study.get("description", "RM CRANIO")
        ds.SeriesDescription = ds.ProtocolName = description
        ds.SeriesNumber, ds.InstanceNumber = series_number, k + 1
        ds.ImageType = list(image_type)
        ds.MRAcquisitionType = mr_type or ("3D" if thickness <= 1.2 and gap == 0 else "2D")
        ds.ScanningSequence, ds.SequenceVariant = "GR", "SP"
        ds.RepetitionTime, ds.EchoTime, ds.FlipAngle = 2300, 2.98, 9
        ds.FrameOfReferenceUID = for_uid
        ds.PatientPosition = "HFS"
        ds.ImageOrientationPatient = [float(x) for x in (*rows, *cols)]
        ds.ImagePositionPatient = [float(x) for x in origin]
        ds.PixelSpacing = [float(px[0]), float(px[1])]
        ds.SliceThickness, ds.SpacingBetweenSlices = float(thickness), float(step)
        ds.SliceLocation = float(origin @ normal)
        ds.Rows, ds.Columns = n_r, n_c
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
        for k2, v in (extra or {}).items():
            setattr(ds, k2, v)
        ds.PixelData = pix.tobytes()
        ds.save_as(out / f"IM{k + 1:04d}", enforce_file_format=True)
        written += 1
    return written
