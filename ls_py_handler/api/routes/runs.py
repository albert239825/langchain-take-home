import uuid
from typing import Any, Dict, List, Optional

import asyncpg
import orjson
from aiobotocore.session import get_session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import UUID4, BaseModel, Field

from ls_py_handler.config.settings import settings
from ls_py_handler.utils.batch_serializer import build_batch_with_offsets

router = APIRouter(prefix="/runs", tags=["runs"])


class Run(BaseModel):
    id: Optional[UUID4] = Field(default_factory=uuid.uuid4)
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


async def get_db_conn():
    """Get a database connection."""
    conn = await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def get_s3_client():
    """Get an S3 client for MinIO."""
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    ) as client:
        yield client


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_runs(
    runs: List[Run],
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """
    Create new runs in batch.

    Takes a JSON array of Run objects, uploads them to MinIO,
    and stores references to certain fields in PostgreSQL.
    """
    if not runs:
        raise HTTPException(status_code=400, detail="No runs provided")

    # Prepare the batch for S3 upload
    batch_id = str(uuid.uuid4())
    object_key = f"batches/{batch_id}.json"
    batch_data, records = build_batch_with_offsets(
        runs,
        object_key,
    )

    # Upload the batch data
    await s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Body=batch_data,
        ContentType="application/json",
    )

    await db.copy_records_to_table(
        "runs",
        records=records,
        columns=["id", "trace_id", "name", "s3_key", "start_offset", "end_offset"],
    )

    inserted_ids = [str(run.id) for run in runs]
    return {"status": "created", "run_ids": inserted_ids}


@router.get("/{run_id}", status_code=status.HTTP_200_OK)
async def get_run(
    run_id: UUID4,
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """
    Get a run by its ID.
    """
    # Fetch the run index from PG
    row = await db.fetchrow(
        """
        SELECT id, trace_id, name, s3_key, start_offset, end_offset
        FROM runs
        WHERE id = $1
        """,
        run_id,
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"Run with ID {run_id} not found")

    start_offset = row["start_offset"]
    end_offset = row["end_offset"]
    byte_range = f"bytes={start_offset}-{end_offset - 1}"

    try:
        response = await s3.get_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=row["s3_key"],
            Range=byte_range,
        )
        async with response["Body"] as stream:
            payload = await stream.read()
        return orjson.loads(payload)
    except Exception as e:
        print(f"Error fetching S3 object with range: {e}")
        return {}
