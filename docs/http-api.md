# HTTP API

Nemorosa provides a comprehensive HTTP API for integration with external tools, automation systems, and custom workflows.

## Base URL

```
http://localhost:8256
```

## Authentication

### API Key Authentication

Include the API key in the Authorization header:

```bash
curl -H "Authorization: Bearer your-api-key" \
     http://localhost:8256/api/webhook?infohash=abc123
```

### No Authentication

If no API key is configured, all endpoints are publicly accessible:

```bash
curl http://localhost:8256/api/webhook?infohash=abc123
```

## Endpoints

### Root Information

#### `GET /`

Get server information and available endpoints.

**Response:**
```json
{
  "message": "Nemorosa Web Server",
  "version": "0.1.0",
  "endpoints": {
    "webhook": "/api/webhook",
    "announce": "/api/announce",
    "job": "/api/job",
    "docs": "/docs"
  }
}
```

### Webhook Processing

#### `POST /api/webhook`

Process a single torrent by infohash.

**Parameters:**

- `infohash` (query, required) - Torrent infohash

**Example:**
```bash
curl -X POST "http://localhost:8256/api/webhook?infohash=abc123def456" \
     -H "Authorization: Bearer your-api-key"
```

**Response:**
```json
{
  "status": "success",
  "message": "Successfully processed torrent: Artist - Album (2024) [FLAC] (abc123def456)"
}
```

**Status Codes:**

- `200 OK` - Successfully processed (injected/saved/already exists)
- `204 No Content` - No matching torrent found (normal case)
- `401 Unauthorized` - Invalid API key
- `500 Internal Server Error` - Processing error

**Response on Error (500):**
```json
{
  "detail": "Processing error: error message here"
}
```

### Announce Processing

#### `POST /api/announce`

Process torrent announce from external systems.

**Request Body:**
```json
{
  "name": "Artist - Album (2024) [FLAC]",
  "link": "https://tracker.example.com/torrents.php?id=12345",
  "album": "Album"
}
```

**Example:**
```bash
curl -X POST "http://localhost:8256/api/announce" \
     -H "Authorization: Bearer your-api-key" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Artist - Album (2024) [FLAC]",
       "link": "https://tracker.example.com/torrents.php?id=12345",
       "album": "Album"
     }'
```

**Response:**
```json
{
  "status": "success",
  "message": "Successfully processed reverse announce torrent: Artist - Album (2024) [FLAC]"
}
```

**Status Codes:**

- `200 OK` - Successfully processed
- `204 No Content` - No matching torrent found or skipped
- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Invalid API key
- `500 Internal Server Error` - Processing error

**Response on Error (400/500):**
```json
{
  "detail": "Error description"
}
```

### Job Management

#### `POST /api/job`

Trigger a scheduled job to run early.

**Parameters:**

- `job_type` (query, required) - Job type: `search` or `cleanup`

**Example:**
```bash
curl -X POST "http://localhost:8256/api/job?job_type=search" \
     -H "Authorization: Bearer your-api-key"
```

**Response:**
```json
{
  "status": "success",
  "message": "Job triggered successfully",
  "job_name": "search",
  "next_run": "2024-01-15T10:30:00",
  "last_run": "2024-01-15T06:30:00"
}
```

**Status Codes:**

- `200 OK` - Job triggered successfully
- `401 Unauthorized` - Invalid API key
- `404 Not Found` - Job not found or disabled
- `409 Conflict` - Job already running or not eligible
- `500 Internal Server Error` - Trigger error

**Response on Error (404/409):**
```json
{
  "detail": "Error message"
}
```

#### `GET /api/job`

Get status of a scheduled job.

**Parameters:**

- `job_type` (query, required) - Job type: `search` or `cleanup`

**Example:**
```bash
curl "http://localhost:8256/api/job?job_type=search" \
     -H "Authorization: Bearer your-api-key"
```

**Response:**
```json
{
  "status": "active",
  "message": "Job is active and scheduled",
  "job_name": "search",
  "next_run": "2024-01-15T10:30:00",
  "last_run": "2024-01-15T06:30:00"
}
```

**Status Codes:**

- `200 OK` - Success
- `400 Bad Request` - Invalid job type
- `401 Unauthorized` - Invalid API key
- `404 Not Found` - Job not found
- `500 Internal Server Error` - Status error

## Response Formats

### ProcessResponse (Webhook & Announce)

Used by `/api/webhook` and `/api/announce` endpoints.

```json
{
  "status": "success|not_found|error|skipped|skipped_potential_trump",
  "message": "status message"
}
```

**Status values:**

- `success` - Torrent processed successfully
- `not_found` - No cross-seeding opportunity found
- `skipped` - Already processed or filtered out
- `skipped_potential_trump` - Potential conflict detected
- `error` - Processing failed

### JobResponse

Used by `/api/job` endpoints.

```json
{
  "status": "success|not_found|conflict|error",
  "message": "Human-readable message",
  "job_name": "search|cleanup",
  "next_run": "2024-01-15T10:30:00",
  "last_run": "2024-01-15T06:30:00"
}
```

**Note:** `next_run` and `last_run` fields may be `null` if not available.

### Error Response

Used for HTTP errors (400, 401, 404, 409, 500).

```json
{
  "detail": "Error description"
}
```

## Status Codes

### HTTP Status Codes

- `200 OK` - Request successful
- `204 No Content` - No matching torrent found (normal case for webhook/announce)
- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Authentication required or invalid API key
- `404 Not Found` - Resource not found
- `409 Conflict` - Resource conflict (e.g., job already running)
- `500 Internal Server Error` - Server error