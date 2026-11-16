from __future__ import annotations

from app.infrastructure.db.common import utcnow as utcnow
from app.infrastructure.db.migrations import SCHEMA as SCHEMA
from app.infrastructure.db.migrations import SCHEMA_VERSION as SCHEMA_VERSION
from app.infrastructure.db.repositories.accounts import AccountsRepository
from app.infrastructure.db.repositories.cdn import CDNRepository
from app.infrastructure.db.repositories.channel import ChannelRepository
from app.infrastructure.db.repositories.cleanup import CleanupRepository
from app.infrastructure.db.repositories.credentials import CredentialsRepository
from app.infrastructure.db.repositories.images import ImagesRepository
from app.infrastructure.db.repositories.shares import SharesRepository
from app.infrastructure.db.repositories.traffic import TrafficRepository
from app.infrastructure.db.repositories.trash import TrashRepository
from app.infrastructure.db.repositories.uploads import UploadsRepository


class Database(
    TrashRepository,
    CleanupRepository,
    ChannelRepository,
    ImagesRepository,
    CDNRepository,
    AccountsRepository,
    CredentialsRepository,
    TrafficRepository,
    UploadsRepository,
    SharesRepository,
):
    """Metadata store with one connection and one lock shared by all repositories."""
