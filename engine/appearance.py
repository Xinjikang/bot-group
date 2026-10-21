"""Single durable viewport background, independent of browser and chat caches."""
import io
import os
import tempfile
import threading
import warnings

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from . import config

MAX_BYTES = 10 * 1024 * 1024
_lock = threading.Lock()


def path():
    return config.data_dir() / 'appearance' / 'background'


def image_type(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 25_000_000:
                    raise ValueError('Unsupported format or dimensions')
                mime = Image.MIME[image.format]
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return mime
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise HTTPException(400, '请选择有效的 PNG、JPEG 或 WebP 图片，最多 2500 万像素。') from exc


def read():
    with _lock:
        try:
            return path().read_bytes()
        except FileNotFoundError:
            return None


def save(data, *, migrate=False):
    if len(data) > MAX_BYTES:
        raise HTTPException(413, '图片最大为 10 MiB。')
    if data:
        image_type(data)
    with _lock:
        target = path()
        if migrate and target.exists():
            raise HTTPException(412, '服务端已有背景设置。')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    # Empty file = explicit reset, preventing stale browser migration.
