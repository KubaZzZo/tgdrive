from __future__ import annotations

import hashlib
import secrets
import uuid

from app.infrastructure.db.common import utcnow
from app.infrastructure.db.session import SQLiteSession


class CredentialsRepository(SQLiteSession):
    def list_client_credentials(self, chat_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,label,scope,created_at FROM client_credentials "
                "WHERE chat_id=? ORDER BY created_at DESC, id",
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_client_credential(self, chat_id: str, label: str, scope: str) -> dict[str, str]:
        secret = secrets.token_urlsafe(32)
        credential = {
            "id": uuid.uuid4().hex,
            "label": label,
            "scope": scope,
            "created_at": utcnow(),
        }
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO client_credentials(id,chat_id,label,scope,digest,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    credential["id"],
                    chat_id,
                    label,
                    scope,
                    hashlib.sha256(secret.encode()).hexdigest(),
                    credential["created_at"],
                ),
            )
        return {**credential, "secret": secret}

    def revoke_client_credential(self, chat_id: str, credential_id: str) -> bool:
        with self._lock, self.conn:
            return bool(
                self.conn.execute(
                    "DELETE FROM client_credentials WHERE chat_id=? AND id=?",
                    (chat_id, credential_id),
                ).rowcount
            )

    def accepts_client_credential(self, chat_id: str, scope: str, secret: str) -> bool:
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self._lock:
            return (
                self.conn.execute(
                    "SELECT 1 FROM client_credentials WHERE digest=? AND scope=? AND chat_id=?",
                    (digest, scope, chat_id),
                ).fetchone()
                is not None
            )
