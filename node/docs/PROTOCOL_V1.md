# SOLVIO Node Protocol V1

`SOLVIO_NODE_PROTOCOL = 1`. Transport-agnostic envelope; HTTP is one carrier.

## Request
| field | type | notes |
|---|---|---|
| `protocol_version` | int | must equal 1 → else `PROTOCOL_MISMATCH` |
| `request_id` | str | client-generated id (uuid hex) |
| `target_node_id` | str? | expected node; mismatch → `VALIDATION_ERROR` |
| `capability` | str | registered id; unknown → `UNKNOWN_CAPABILITY` |
| `operation` | str | default `invoke` |
| `timestamp` | str | ISO-8601 UTC |
| `payload` | object | validated against the capability input schema |

Over HTTP, `POST /v1/capabilities/{id}/invoke` — the URL `{id}` is authoritative for
`capability`; the body carries `request_id`/`target_node_id`/`payload`.

## Response
| field | type | notes |
|---|---|---|
| `protocol_version` | int | 1 |
| `request_id` | str | echoed |
| `node_id` | str | responding node |
| `capability` | str | |
| `status` | `ok` \| `error` | |
| `result` | object? | on success (capability output schema) |
| `duration_ms` | float | server-measured |
| `error_code` | enum? | see below |
| `error_message` | str? | **payload-free** (never echoes input) |

## Error codes → HTTP
```
PROTOCOL_MISMATCH   400    UNKNOWN_CAPABILITY  404
VALIDATION_ERROR    400    PAYLOAD_TOO_LARGE   413
RESOURCE_BUSY       503    TIMEOUT             504
CAPABILITY_ERROR    500    INTERNAL_ERROR      500    UNAUTHENTICATED 401
```

## Privacy
Payload/result **content** never appears in logs, errors, or metrics — only
`request_id`, `capability`, `status`, `duration_ms`, `payload_size`, `result_size`.
