"""Before/after pictures of the head surface, to check defacing by eye. No text in the pictures."""
import numpy as np
import nibabel as nib
from scipy import ndimage as ndi
from PIL import Image


def otsu(values, bins=256):
    hist, edges = np.histogram(values, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers) / np.maximum(w0, 1)
    m1 = (np.sum(hist * centers) - np.cumsum(hist * centers)) / np.maximum(w1, 1)
    return centers[np.argmax(w0 * w1 * (m0 - m1) ** 2)]


def head_mask(path, threshold=None):
    """Head mask at 1 mm in RAS orientation, and the intensity threshold used (reuse it for the 'after' image)."""
    img = nib.as_closest_canonical(nib.load(str(path)))
    data = np.asanyarray(img.dataobj).astype(np.float32)
    if data.ndim == 4:
        data = data[..., 0]
    data = ndi.gaussian_filter(ndi.zoom(data, img.header.get_zooms()[:3], order=1), 1.2)
    if threshold is None:
        threshold = 0.35 * otsu(data[data > 0])
    mask = data > threshold
    lab, n = ndi.label(mask)
    if n > 1:
        mask = lab == (np.argmax(ndi.sum(mask, lab, range(1, n + 1))) + 1)
    return ndi.binary_fill_holes(mask), threshold


def view(mask, yaw, size):
    """Shaded surface seen from the front after turning the head by yaw degrees."""
    m = ndi.rotate(mask.astype(np.float32), yaw, axes=(0, 1), reshape=True, order=1)
    f = ndi.gaussian_filter(m, 1.4)[:, ::-1, :]
    c = np.maximum.accumulate(f, axis=1)
    ok = c[:, -1, :] > 0.5
    h = -(1 - c).sum(axis=1)
    h, ok = h.T[::-1, ::-1], ok.T[::-1, ::-1]
    if not ok.any():
        return Image.new("RGB", (size, size), "white")
    h = ndi.gaussian_filter(np.where(ok, h, h[ok].min()), 0.7)
    gy, gx = np.gradient(h)
    n = np.dstack([-gx, -gy, np.ones_like(h)])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    light = np.array([-0.45, -0.55, 1.0])
    light /= np.linalg.norm(light)
    shade = np.clip(0.28 + 0.72 * np.clip(n @ light, 0, 1), 0, 1)
    rgb = np.where(ok[..., None], shade[..., None] * np.array([236, 205, 180]) / 255.0, 1.0)
    im = Image.fromarray((rgb * 255).astype(np.uint8)).crop(Image.fromarray((ok * 255).astype(np.uint8)).getbbox())
    k = 0.94 * min(size / im.width, size / im.height)
    im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return canvas


def before_after(before, after, out, size=360):
    """Top row: before (front-oblique, side). Bottom row: after."""
    mb, t = head_mask(before)
    ma, _ = head_mask(after, t)
    grid = Image.new("RGB", (2 * size + 30, 2 * size + 30), "white")
    for r, m in enumerate((mb, ma)):
        for c, yaw in enumerate((35, 90)):
            grid.paste(view(m, yaw, size), (10 + c * (size + 10), 10 + r * (size + 10)))
    grid.save(out)
