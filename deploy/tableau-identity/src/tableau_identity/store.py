"""Хранилище 1:1-маппинга «ИИ-пользователь → учётная запись Tableau (PAT)».

Требование ТС (п.4.3, 5.4.2): персональная учётка Tableau — персональный PAT,
с наследованием прав (RLS). Требование ТС (п.4.6, 5.5.4): секреты хранятся в
защищённом виде, не в открытом.

Поэтому на диске PAT-секрет всегда лежит зашифрованным (Fernet, ключ — из env
`TABLEAU_IDENTITY_ENC_KEY`, рядом с данными не хранится). В памяти — расшифрован
только для брокера, который логинится им в Tableau. Наружу (в админ-API) секрет
не отдаётся никогда.

Файл — источник правды: админка пишет его атомарно (rw), брокер читает (ro) с
перечитыванием по mtime.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet, InvalidToken

from . import pgstore as _pg


@dataclass(frozen=True)
class Mapping:
    """Одна привязка. `pat_secret` в памяти расшифрован; наружу не сериализуется."""

    user: str
    tableau_username: str
    pat_name: str
    pat_secret: str

    def public(self) -> dict[str, str]:
        """Представление для админ-API/UI — без секрета."""
        return {
            "user": self.user,
            "tableau_username": self.tableau_username,
            "pat_name": self.pat_name,
        }


class MappingError(ValueError):
    """Нарушение инварианта маппинга (например, 1:1 по учётке Tableau)."""


def generate_key() -> str:
    """Сгенерировать валидный Fernet-ключ (urlsafe base64, 32 байта)."""
    return Fernet.generate_key().decode()


class MappingStore:
    """Хранилище маппинга с шифрованием секретов. Бэкенд — Postgres (если задан
    `db_url`) или YAML-файл. Шифрование/1:1 — здесь; бэкенд хранит только шифртекст.

    Брокер читает горячо (на каждый tool-вызов): в файловом режиме — reload по
    mtime, в PG — короткий TTL-кэш (не бьём БД на каждый вызов).
    ponytail: per-call кэш TTL; если понадобится строгая консистентность — LISTEN/NOTIFY.
    """

    def __init__(self, path: str | Path, enc_key: str, db_url: str | None = None):
        self._path = Path(path)
        self._db = db_url or None
        try:
            self._fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "TABLEAU_IDENTITY_ENC_KEY невалиден: нужен Fernet-ключ "
                "(urlsafe base64, 32 байта). Сгенерировать: `make identity-key`."
            ) from exc
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._loaded_at: float = float("-inf")
        self._ttl = float(os.environ.get("IDENTITY_DB_TTL_SECONDS", 3))
        self._by_user: dict[str, Mapping] = {}
        if self._db:
            _pg.init(self._db)

    # ── чтение ────────────────────────────────────────────────────────────
    def _ensure_fresh(self) -> None:
        if self._db:
            if time.monotonic() - self._loaded_at < self._ttl:
                return
            raw = _pg.read_all(self._db)
            with self._lock:
                self._by_user = self._decrypt_raw(raw)
                self._loaded_at = time.monotonic()
            return
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._by_user = {}
                self._mtime = -1.0
            return
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            entries = data.get("mappings", []) if isinstance(data, dict) else []
            raw = {str(e["user"]): dict(e) for e in (entries or [])
                   if isinstance(e, dict) and e.get("user")}
            self._by_user = self._decrypt_raw(raw)
            self._mtime = mtime

    def _decrypt_raw(self, raw: dict[str, dict[str, Any]]) -> dict[str, Mapping]:
        result: dict[str, Mapping] = {}
        for user, entry in raw.items():
            enc = entry.get("pat_secret_enc")
            if not user or not enc:
                continue
            try:
                secret = self._fernet.decrypt(str(enc).encode()).decode()
            except InvalidToken as exc:
                # Чужой/битый ключ — отказ, а не «пропустить»: иначе увели бы на fallback-креды.
                raise RuntimeError(
                    f"Не удалось расшифровать PAT для '{user}': "
                    "TABLEAU_IDENTITY_ENC_KEY не совпадает с тем, которым шифровали."
                ) from exc
            result[user] = Mapping(
                user=user,
                tableau_username=str(entry.get("tableau_username", user)),
                pat_name=str(entry.get("pat_name", "")),
                pat_secret=secret,
            )
        return result

    def get(self, user: str) -> Mapping | None:
        self._ensure_fresh()
        return self._by_user.get(user)

    def list(self) -> list[Mapping]:
        self._ensure_fresh()
        return sorted(self._by_user.values(), key=lambda m: m.user)

    # ── запись (только админка) ───────────────────────────────────────────
    def put(self, user: str, tableau_username: str, pat_name: str, pat_secret: str) -> None:
        user = user.strip()
        tableau_username = tableau_username.strip()
        pat_name = pat_name.strip()
        if not (user and tableau_username and pat_name and pat_secret):
            raise MappingError("user, tableau_username, pat_name и pat_secret обязательны.")
        enc = self._fernet.encrypt(pat_secret.encode()).decode()
        if self._db:
            conflict = _pg.upsert(self._db, user, tableau_username, pat_name, enc)
            if conflict:
                raise MappingError(
                    f"Учётка Tableau '{tableau_username}' уже привязана к '{conflict}' (нужно 1:1)."
                )
            self._loaded_at = float("-inf")  # инвалидируем кэш
            return
        with self._lock:
            current = self._read_raw_file()
            for other, entry in current.items():  # 1:1 в обе стороны
                if other != user and entry.get("tableau_username") == tableau_username:
                    raise MappingError(
                        f"Учётка Tableau '{tableau_username}' уже привязана к '{other}' (нужно 1:1)."
                    )
            current[user] = {
                "user": user, "tableau_username": tableau_username,
                "pat_name": pat_name, "pat_secret_enc": enc,
            }
            self._write_raw_file(current)

    def delete(self, user: str) -> bool:
        if self._db:
            ok = _pg.delete(self._db, user)
            self._loaded_at = float("-inf")
            return ok
        with self._lock:
            current = self._read_raw_file()
            if user not in current:
                return False
            del current[user]
            self._write_raw_file(current)
            return True

    def migrate_from_file_if_empty(self) -> int:
        """Разовый перенос привязок из YAML в пустую БД. Шифртекст секрета
        переносится как есть (без пере-шифрования). Возвращает число записей."""
        if not self._db or not self._path.exists() or not _pg.is_empty(self._db):
            return 0
        raw = self._read_raw_file()
        n = 0
        for user, e in raw.items():
            enc = e.get("pat_secret_enc")
            if not enc:
                continue
            _pg.upsert(self._db, user, str(e.get("tableau_username", user)),
                       str(e.get("pat_name", "")), str(enc))
            n += 1
        self._loaded_at = float("-inf")
        return n

    # ── сырой YAML на диске (секреты остаются зашифрованными) ──────────────
    def _read_raw_file(self) -> dict[str, dict[str, Any]]:
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {}
        entries = data.get("mappings", []) if isinstance(data, dict) else []
        return {str(e["user"]): dict(e) for e in (entries or [])
                if isinstance(e, dict) and e.get("user")}

    def _write_raw_file(self, current: dict[str, dict[str, Any]]) -> None:
        payload = {"mappings": [current[u] for u in sorted(current)]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yml.tmp")
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tmp.replace(self._path)
        self._mtime = -1.0  # заставить брокер перечитать при следующем get()


def default_store() -> MappingStore:
    key = os.environ.get("TABLEAU_IDENTITY_ENC_KEY")
    if not key:
        raise RuntimeError(
            "TABLEAU_IDENTITY_ENC_KEY не задан. Сгенерировать ключ: `make identity-key`."
        )
    return MappingStore(
        os.environ.get("MAPPINGS_PATH", "/app/catalog/mappings.yml"),
        key,
        db_url=os.environ.get("IDENTITY_DATABASE_URL"),
    )
