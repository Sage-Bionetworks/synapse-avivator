# synapse-avivator

View Synapse-hosted OME-TIFF images in [Avivator](https://avivator.gehlenborglab.org/) with transparent presigned URL refresh.

![Multiplexed fluorescence image viewed in Avivator via synapse-avivator proxy](docs/images/avivator-demo.png)

Synapse presigned URLs expire after 15 minutes. Byte-range image viewers like Avivator/Viv make many small HTTP requests over a session, so URLs baked in at load time go stale. This tool runs a local proxy that refreshes URLs automatically mid-session, with a two-tier cache for fast tile revisits.

## Install

```bash
pip install synapse-avivator
```

Or run directly without installing:

```bash
uvx synapse-avivator syn51671125
```

## Usage

```bash
# View a specific Synapse entity
synapse-avivator syn51671125

# Start the server, enter entity IDs in the browser UI
synapse-avivator

# Custom port
synapse-avivator --port 9000 syn51671125

# Verbose logging (writes session logs to logs/)
synapse-avivator -v syn51671125
```

## Authentication

synapse-avivator reads your Synapse credentials automatically. Set up once with:

```bash
pip install synapseclient
synapse config
```

This creates `~/.synapseConfig` with your personal access token. Alternatively:

```bash
# Environment variable
export SYNAPSE_AUTH_TOKEN=your-token-here
synapse-avivator syn51671125

# CLI flag (visible in process list — prefer env var or config file)
synapse-avivator --token your-token-here syn51671125
```

## How it works

```
Browser (Avivator)
    |
    |  GET /image/syn51671125.ome.tiff
    |  Range: bytes=1048576-2097151
    v
Local Proxy (localhost:8000)
    |
    |  1. Check two-tier LRU cache (block cache + tile cache)
    |  2. Cache miss → get presigned URL from Synapse
    |  3. Forward Range request to S3
    |  4. Cache response for future requests
    |  5. Auto-refresh URL before 15-min expiry
    |  6. Retry once on 403 (expired URL)
    v
S3 (presigned URL)
```

**Cache tiers:**
- **Block cache (256 KB):** Absorbs GeoTIFF.js's 1-byte probe + re-read pattern
- **Tile cache (up to 5 MB):** Caches tile responses for viewport revisits

## Requirements

- Python 3.10+
- A Synapse account with access to the target files
- Source OME-TIFF files must be **tiled** (512x512) for interactive viewing. Untiled files will load but require downloading full planes.

## Offsets sidecar

For large OME-TIFFs (>1 GB), an `.offsets.json` sidecar dramatically speeds up initial load by pre-computing IFD locations:

```bash
# Generate from a local copy of the file
python -c "
import json, tifffile
with tifffile.TiffFile('local_copy.ome.tiff') as tif:
    json.dump([int(p.offset) for p in tif.pages], open('syn12345.offsets.json', 'w'))
"
```

Place the `synXXXXX.offsets.json` file in the directory where you run `synapse-avivator`. The proxy serves it automatically.

## Development

```bash
git clone https://github.com/Sage-Bionetworks/synapse-avivator.git
cd synapse-avivator
pip install -e ".[dev]"
pytest tests/
```
