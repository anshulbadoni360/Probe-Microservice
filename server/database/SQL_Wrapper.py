from utils.ServerLogger import ServerLogger
import os, ssl
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

logger = ServerLogger()

SQL_DATABASE_URL = os.getenv("SQL_DATABASE_URL", "")
connect_args = {}

if not SQL_DATABASE_URL:
    logger.error("SQL_DATABASE_URL is MISSING from environment!", logger.danger)
else:
    parsed = urlparse(SQL_DATABASE_URL)
    logger.info(f"🔗 Connecting to SQL at {parsed.hostname}")

    # asyncmy doesn't accept ssl-mode as a URL query param (that's MySQL CLI syntax).
    # Strip it and convert to a proper ssl context via connect_args.
    qs = parse_qs(parsed.query, keep_blank_values=True)
    ssl_mode = qs.pop("ssl-mode", [None])[0]
    clean_query = urlencode({k: v[0] for k, v in qs.items()})
    SQL_DATABASE_URL = urlunparse(parsed._replace(query=clean_query))

    if ssl_mode and ssl_mode.upper() in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        connect_args = {"ssl": ctx}

engine = create_async_engine(
    SQL_DATABASE_URL,
    echo=False,
    future=True,
    connect_args=connect_args,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
