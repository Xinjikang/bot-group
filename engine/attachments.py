"""Durable image originals stored beside the engine database. No TTL."""
import hashlib
import io
import json
import os
import uuid
import warnings
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from . import db

MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 4


def root():
    # req#122：附件目录走统一配置层（GROUP_ATTACHMENTS_DIR 覆盖，默认随 db 目录）。
    from . import config
    return config.attachments_dir()


def store(conn, data, filename, project, owner):
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Image must be between 1 byte and 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                fmt, width, height = img.format, img.width, img.height
                if fmt not in {"PNG", "JPEG", "WEBP"} or width * height > 25_000_000:
                    raise ValueError("Unsupported image format or dimensions")
                img.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise HTTPException(400, "Use a valid PNG, JPEG or WebP image, at most 25 megapixels") from exc
    aid = uuid.uuid4().hex
    ext, mime = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                 "WEBP": ("webp", "image/webp")}[fmt]
    root().mkdir(parents=True, exist_ok=True)
    path = root() / f"{aid}.{ext}"
    name = filename.replace("\\", "/").split("/")[-1][:200] or path.name
    try:
        with path.open("xb") as stream:
            stream.write(data)
        conn.execute("INSERT INTO attachments(id, project, owner, filename, stored_name, mime, size, sha256, width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, project, owner, name, path.name, mime, len(data),
                      hashlib.sha256(data).hexdigest(), width, height, db.now_iso()))
        conn.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return metadata(conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone())


def metadata(row):
    return {key: row[key] for key in
            ("id", "filename", "mime", "size", "sha256", "width", "height")}


def bind(conn, ids, message_id, project, owner):
    for position, aid in enumerate(ids):
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        if row is None or row["project"] != project or row["owner"] != owner or row["message_id"] is not None:
            raise HTTPException(400, "Attachment is unavailable, already sent, or belongs to another project/sender")
        conn.execute("UPDATE attachments SET message_id=?, position=? WHERE id=?", (message_id, position, aid))


def enrich(conn, rows):
    messages = [dict(row) for row in rows]
    by_id = {}
    for message in messages:
        mid = message.get("message_id", message.get("id"))
        by_id[mid] = message
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(f"SELECT * FROM attachments WHERE message_id IN ({placeholders}) ORDER BY message_id,position", batch):
            by_id[row["message_id"]].setdefault("attachments", []).append(metadata(row))
    return messages


def accessible(conn, aid, identity, kind):
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if row is None:
        raise HTTPException(404, "Attachment not found")
    if kind == "bot":
        from .access import require_project
        require_project(conn, identity, row["project"])
        allowed = row["owner"] == identity
        if row["message_id"] is not None:
            msg = conn.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
            if msg["thread_id"] is not None:
                thr = conn.execute("SELECT members FROM threads WHERE id=?", (msg["thread_id"],)).fetchone()
                allowed = allowed or identity in json.loads(thr["members"])
            else:
                allowed = allowed or conn.execute("SELECT 1 FROM inbox WHERE message_id=? AND profile=?", (row["message_id"], identity)).fetchone() is not None
        if not allowed:
            raise HTTPException(403, "Attachment is outside this member's conversation")
    path = root() / row["stored_name"]
    if not path.is_file():
        raise HTTPException(410, "Attachment original is missing; restore it from backup")
    return row, path
