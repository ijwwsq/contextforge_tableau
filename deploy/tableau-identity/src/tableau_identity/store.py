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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet, InvalidToken


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
    """Файловое хранилище маппинга с шифрованием секретов и reload по mtime."""

    def __init__(self, path: str | Path, enc_key: str):
        self._path = Path(path)
        try:
            self._fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "TABLEAU_IDENTITY_ENC_KEY невалиден: нужен Fernet-ключ "
                "(urlsafe base64, 32 байта). Сгенерировать: `make identity-key`."
            ) from exc
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._by_user: dict[str, Mapping] = {}

    # ── чтение ────────────────────────────────────────────────────────────
    def _reload_if_changed(self) -> None:
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
            self._by_user = self._parse(self._path.read_text(encoding="utf-8"))
            self._mtime = mtime

    def _parse(self, text: str) -> dict[str, Mapping]:
        data = yaml.safe_load(text) or {}
        entries = data.get("mappings", []) if isinstance(data, dict) else []
        result: dict[str, Mapping] = {}
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            user = str(entry.get("user", "")).strip()
            enc = entry.get("pat_secret_enc")
            if not user or not enc:
                continue
            try:
                secret = self._fernet.decrypt(str(enc).encode()).decode()
            except InvalidToken as exc:
                # Чужой/битый ключ — это отказ, а не «пропустить»: молчаливое
                # игнорирование увело бы юзера на fallback-креды.
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
        self._reload_if_changed()
        return self._by_user.get(user)

    def list(self) -> list[Mapping]:
        self._reload_if_changed()
        return sorted(self._by_user.values(), key=lambda m: m.user)

    # ── запись (только админка) ───────────────────────────────────────────
    def put(self, user: str, tableau_username: str, pat_name: str, pat_secret: str) -> None:
        user = user.strip()
        tableau_username = tableau_username.strip()
        pat_name = pat_name.strip()
        if not (user and tableau_username and pat_name and pat_secret):
            raise MappingError("user, tableau_username, pat_name и pat_secret обязательны.")
        with self._lock:
            current = self._read_raw()
            # 1:1 в обе стороны: одна учётка Tableau не может висеть на двух юзерах.
            for other, entry in current.items():
                if other != user and entry.get("tableau_username") == tableau_username:
                    raise MappingError(
                        f"Учётка Tableau '{tableau_username}' уже привязана к '{other}' (нужно 1:1)."
                    )
            current[user] = {
                "user": user,
                "tableau_username": tableau_username,
                "pat_name": pat_name,
                "pat_secret_enc": self._fernet.encrypt(pat_secret.encode()).decode(),
            }
            self._write_raw(current)

    def delete(self, user: str) -> bool:
        with self._lock:
            current = self._read_raw()
            if user not in current:
                return False
            del current[user]
            self._write_raw(current)
            return True

    # ── сырой YAML на диске (секреты остаются зашифрованными) ──────────────
    def _read_raw(self) -> dict[str, dict[str, Any]]:
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {}
        entries = data.get("mappings", []) if isinstance(data, dict) else []
        return {
            str(e["user"]): dict(e)
            for e in (entries or [])
            if isinstance(e, dict) and e.get("user")
        }

    def _write_raw(self, current: dict[str, dict[str, Any]]) -> None:
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
    return MappingStore(os.environ.get("MAPPINGS_PATH", "/app/catalog/mappings.yml"), key)
