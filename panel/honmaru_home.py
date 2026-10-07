"""Personal homepage data, separate from game configuration."""

import base64
import binascii
import json
import os
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

PROFILE_LIMITS = {
    "honmaru_name": 40, "saniwa_name": 40, "province": 30,
    "attendant": 40, "motto": 120, "joined_on": 10, "avatar": 710000,
}


class HomeStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {"schema_version": 1, "profile": {}, "notes": []}
            # Never replace damaged or newer data with empty defaults.
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("profile"), dict) or not isinstance(data.get("notes"), list):
                raise ValueError("本丸档案暂时无法读取，请保留原文件后检查。")
            return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if self.path.exists():
            self.path.with_suffix(".json.bak").write_bytes(self.path.read_bytes())
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def save_profile(self, profile):
        if not isinstance(profile, dict) or set(profile) - set(PROFILE_LIMITS):
            raise ValueError("档案字段不正确。")
        clean = {}
        for key, value in profile.items():
            if not isinstance(value, str) or len(value) > PROFILE_LIMITS[key]:
                raise ValueError("档案内容太长或格式不正确。")
            clean[key] = value.strip()
        joined = clean.get("joined_on")
        if joined:
            try:
                valid = date.fromisoformat(joined).isoformat() == joined and joined <= date.today().isoformat()
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("请填写有效的就任日期，不能晚于今天。")
        avatar = clean.get("avatar")
        if avatar:
            try:
                prefix, payload = avatar.split(",", 1)
                raw = base64.b64decode(payload, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("头像图片格式不正确。") from exc
            signatures = {
                "data:image/png;base64": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                "data:image/jpeg;base64": raw.startswith(b"\xff\xd8\xff"),
                "data:image/webp;base64": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
            }
            if not signatures.get(prefix) or len(raw) > 512 * 1024:
                raise ValueError("请选择 512 KB 以内的 PNG、JPG 或 WebP 头像。")
        with self.lock:
            data = self.read()
            data["profile"].update(clean)
            self._write(data)
            return data["profile"]

    def save_note(self, body, note_id=None):
        if not isinstance(body, str) or not body.strip() or len(body) > 2000:
            raise ValueError("小记请填写 1～2000 字。")
        with self.lock:
            data = self.read()
            if note_id:
                note = next((item for item in data["notes"] if item["id"] == note_id), None)
                if note is None:
                    raise LookupError("这条小记没有找到。")
                note.update(body=body.strip(), updated_at=time.time())
            else:
                note = {"id": uuid.uuid4().hex, "body": body.strip(), "created_at": time.time()}
                data["notes"].insert(0, note)
            self._write(data)
            return note


def create_home_router(path: Path, jp_path: Path | None = None):
    router = APIRouter(prefix="/api/honmaru-home")
    store = HomeStore(path)
    jp_store = HomeStore(jp_path) if jp_path is not None else None

    def selected(server):
        if server in ("", "cn"):
            return store
        if server == "jp" and jp_store is not None:
            return jp_store
        raise HTTPException(400, "本丸来源不正确。")

    def call(action, *args):
        try:
            return action(*args)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "本丸档案暂时无法保存，请检查数据目录。") from exc

    @router.get("")
    def read_home(server: str = ""):
        return call(selected(server).read)

    @router.put("/profile")
    async def save_profile(request: Request, server: str = ""):
        return {"profile": call(selected(server).save_profile, await request.json())}

    @router.post("/notes")
    async def add_note(request: Request, server: str = ""):
        body = await request.json()
        return {"note": call(selected(server).save_note, body.get("body") if isinstance(body, dict) else None)}

    @router.put("/notes/{note_id}")
    async def edit_note(note_id: str, request: Request, server: str = ""):
        body = await request.json()
        return {"note": call(selected(server).save_note, body.get("body") if isinstance(body, dict) else None, note_id)}

    return router
