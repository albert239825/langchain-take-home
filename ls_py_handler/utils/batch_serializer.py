from __future__ import annotations

from typing import Iterable, List, Tuple, TYPE_CHECKING, Any

import orjson

if TYPE_CHECKING:
    from ls_py_handler.api.routes.runs import Run


_LIST_START = b"["
_LIST_END = b"]"
_COMMA = b","
_RUN_START = b'{"id":'
_TRACE_PREFIX = b',"trace_id":'
_NAME_PREFIX = b',"name":'
_INPUTS_PREFIX = b',"inputs":'
_OUTPUTS_PREFIX = b',"outputs":'
_METADATA_PREFIX = b',"metadata":'
_RUN_END = b"}"


def _json_payload(value: Any) -> bytes:
    if value is None:
        return orjson.dumps({})
    return orjson.dumps(value)


def build_batch_with_offsets(
    runs: Iterable["Run"],
    bucket: str,
    object_key: str,
) -> Tuple[bytes, List[Tuple[Any, Any, str, str, str, str]]]:
    parts = [_LIST_START]
    current_pos = len(_LIST_START)
    records: List[Tuple[Any, Any, str, str, str, str]] = []
    object_prefix = f"s3://{bucket}/{object_key}"

    for index, run in enumerate(runs):
        if index:
            parts.append(_COMMA)
            current_pos += len(_COMMA)

        parts.append(_RUN_START)
        current_pos += len(_RUN_START)
        run_id_json = orjson.dumps(run.id)
        parts.append(run_id_json)
        current_pos += len(run_id_json)

        parts.append(_TRACE_PREFIX)
        current_pos += len(_TRACE_PREFIX)
        trace_id_json = orjson.dumps(run.trace_id)
        parts.append(trace_id_json)
        current_pos += len(trace_id_json)

        parts.append(_NAME_PREFIX)
        current_pos += len(_NAME_PREFIX)
        name_json = orjson.dumps(run.name)
        parts.append(name_json)
        current_pos += len(name_json)

        inputs_json = _json_payload(run.inputs)
        outputs_json = _json_payload(run.outputs)
        metadata_json = _json_payload(run.metadata)

        parts.append(_INPUTS_PREFIX)
        current_pos += len(_INPUTS_PREFIX)
        inputs_start = current_pos
        parts.append(inputs_json)
        inputs_end = inputs_start + len(inputs_json)
        current_pos = inputs_end

        parts.append(_OUTPUTS_PREFIX)
        current_pos += len(_OUTPUTS_PREFIX)
        outputs_start = current_pos
        parts.append(outputs_json)
        outputs_end = outputs_start + len(outputs_json)
        current_pos = outputs_end

        parts.append(_METADATA_PREFIX)
        current_pos += len(_METADATA_PREFIX)
        metadata_start = current_pos
        parts.append(metadata_json)
        metadata_end = metadata_start + len(metadata_json)
        current_pos = metadata_end

        parts.append(_RUN_END)
        current_pos += len(_RUN_END)

        records.append(
            (
                run.id,
                run.trace_id,
                run.name,
                f"{object_prefix}#{inputs_start}:{inputs_end}/inputs",
                f"{object_prefix}#{outputs_start}:{outputs_end}/outputs",
                f"{object_prefix}#{metadata_start}:{metadata_end}/metadata",
            )
        )

    parts.append(_LIST_END)
    batch_data = b"".join(parts)
    return batch_data, records
