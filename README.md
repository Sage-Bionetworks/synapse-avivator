# synapse-avivator

View Synapse and CRDC-hosted OME-TIFF images in a bundled [Avivator](https://avivator.gehlenborglab.org/) viewer with transparent presigned URL refresh.

![Multiplexed fluorescence image viewed in Avivator via synapse-avivator proxy](docs/images/avivator-demo.png)

Synapse presigned URLs expire after 15 minutes. Byte-range image viewers like Avivator/Viv make many small HTTP requests over a session, so URLs baked in at load time go stale. This tool runs a local proxy that refreshes URLs automatically mid-session, with a two-tier cache for fast tile revisits. It bundles a patched Avivator build so everything stays on your local machine — no tokens or image data leave your origin.

## Install

```bash
pip install git+https://github.com/Sage-Bionetworks/synapse-avivator.git
```

Or run directly without installing:

```bash
uvx --from git+https://github.com/Sage-Bionetworks/synapse-avivator.git synapse-avivator syn74326609
```

## Usage

```bash
# View a specific Synapse entity — opens bundled Avivator at localhost:8000
synapse-avivator syn74326609

# Start the server, enter entity IDs in the browser UI
synapse-avivator

# View a Gen3/CRDC file via DRS URI
synapse-avivator 'drs://nci-crdc.datacommons.io/dg.4DFC/C99353A6-51AB-4181-9910-86466DDF6F6E'

# Hosted mode — users provide their own Synapse PAT in the browser
synapse-avivator --hosted

# Custom port + verbose logging
synapse-avivator -v --port 9000 syn74326609
```

## Authentication

### Synapse

synapse-avivator reads your Synapse credentials automatically. Set up once with:

```bash
pip install synapseclient
synapse config
```

This creates `~/.synapseConfig` with your personal access token. Alternatively:

```bash
# Environment variable
export SYNAPSE_AUTH_TOKEN=your-token-here
synapse-avivator syn74326609
```

### Gen3 / NCI CRDC

To view files from the [NCI Cancer Research Data Commons](https://datacommons.cancer.gov/) via DRS URIs:

1. Install with Gen3 support: `pip install "synapse-avivator[gen3]"`
2. Log in at https://nci-crdc.datacommons.io/identity
3. Go to Profile → "Create API key" → download `credentials.json`
4. Place it at `~/.gen3/credentials.json`

Then pass a DRS URI directly:

```bash
synapse-avivator 'drs://nci-crdc.datacommons.io/dg.4DFC/C99353A6-51AB-4181-9910-86466DDF6F6E'
```

### Hosted mode

For shared deployments where users can't provide local config files:

```bash
synapse-avivator --hosted
```

Users paste their Synapse Personal Access Token in the browser UI. The token is stored in `sessionStorage` (cleared on tab close) and sent to the proxy via a secure HTTP header — never in URLs, never logged, never stored on disk.

## How it works

```
Browser (bundled Avivator at localhost:8000/viewer/)
    |
    |  GET /image/syn74326609.ome.tiff
    |  Range: bytes=1048576-2097151
    |  X-Synapse-Token: <from sessionStorage>
    v
Local Proxy (localhost:8000)
    |
    |  1. Check two-tier LRU cache (block cache + tile cache)
    |  2. Cache miss → get presigned URL from Synapse/Gen3
    |  3. Forward Range request to S3
    |  4. Cache response for future requests
    |  5. Auto-refresh URL before expiry (60s buffer)
    |  6. Retry once on 403 (expired URL)
    v
S3 (presigned URL)
```

**Cache tiers:**
- **Block cache (256 KB):** Absorbs GeoTIFF.js's 1-byte probe + re-read pattern
- **Tile cache (up to 5 MB):** Caches tile responses for viewport revisits
- **Inflight dedup:** Concurrent identical S3 requests are coalesced

**Security:**
- Tokens never appear in URLs or server logs
- Bundled Avivator runs on your origin — no data sent to third parties
- `sessionStorage` cleared on tab close
- Proxy binds to `127.0.0.1` only (not `0.0.0.0`)

## Requirements

- Python 3.10+
- A Synapse account with access to the target files

### Image file requirements

This tool works best with **tiled, pyramidal OME-TIFF** files. Tiling (e.g., 512x512) allows the viewer to fetch only the pixels needed for the current viewport via byte-range requests. Pyramid levels (multi-resolution) enable smooth zooming without downloading full-resolution data.

**Untiled files** will load but perform poorly — the viewer must download entire image planes to display any region, which is impractical for large files.

You can check if your file is tiled with:
```bash
python -c "
import tifffile
with tifffile.TiffFile('your_file.ome.tiff') as tif:
    for i, level in enumerate(tif.series[0].levels):
        page = level.pages[0]
        tile = (getattr(page, 'tilelength', 0), getattr(page, 'tilewidth', 0))
        print(f'Level {i}: {level.shape}  tile={tile}')
"
```

If `tile=(0, 0)`, the file is not tiled and will need to be re-converted for interactive viewing.

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

### Rebuilding the bundled Avivator

The bundled viewer is a patched build of [hms-dbmi/viv](https://github.com/hms-dbmi/viv)'s Avivator. To rebuild:

```bash
git clone https://github.com/hms-dbmi/viv.git /tmp/viv
cd /tmp/viv

# Apply patches: base path + fetch interceptor for token injection
# (see sites/avivator/vite.config.js and sites/avivator/src/index.jsx)

pnpm install
pnpm --filter avivator build
cp -r sites/avivator/dist /path/to/synapse-avivator/src/synapse_avivator/static/viewer
```
