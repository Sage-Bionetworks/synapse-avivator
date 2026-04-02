# Streaming Gigabyte Tissue Images from Synapse Without Downloading Them

If you work with multiplexed imaging data in [HTAN](https://humantumoratlas.org/), you've probably stared at a 25GB OME-TIFF and thought: *there has to be a better way than waiting two hours to download this before I can look at it.*

There is. We built `synapse-avivator`.

## The Problem: URLs That Go Stale Mid-Session

[Avivator](https://avivator.gehlenborglab.org/) is a great open-source viewer for OME-TIFF files — it uses [Viv](https://github.com/hms-dbmi/viv) under the hood to stream tiles via byte-range HTTP requests, so you only fetch the pixels you're actually looking at. This makes it practical to explore multi-gigabyte images without downloading the whole thing.

The catch: Synapse serves files via presigned S3 URLs, and those URLs expire after 15 minutes. Avivator bakes the URL in at load time and keeps making tile requests long after that window closes. Keep zooming around a tissue section for 20 minutes and you start getting cryptic `403 Forbidden` errors, with no clear indication of what went wrong.

This isn't a bug in Avivator — it's a fundamental mismatch between how presigned URLs work and how tile-streaming viewers work. The viewer assumes the URL is stable. Synapse assumes you'll download the file in one shot.

## The Fix: A Local Proxy That Handles URL Refresh Transparently

`synapse-avivator` is a small CLI that runs a local FastAPI server between your browser and S3. One command:

```bash
synapse-avivator syn74326609
```

That's it. It authenticates with Synapse using your existing `~/.synapseConfig`, starts the proxy on `localhost:8000`, and opens Avivator in your browser already pointed at the proxy. From Avivator's perspective, it's just talking to a stable local server that serves byte ranges. The URL refresh happens invisibly.

The proxy keeps track of when presigned URLs will expire and fetches a fresh one from Synapse 60 seconds before the deadline. It also retries automatically on `403` — which catches the race condition right at the expiry boundary. In a live test, a URL expired mid-viewing: the proxy caught the `403`, fetched a new presigned URL, and retried the tile request. Total time: 603ms. The viewer never noticed.

## The Caching Layer

Naive proxying would be slow. Every tile request would hit the Synapse API to get a presigned URL, then hit S3 for the actual data. That's two round trips per tile, which adds up fast.

The proxy uses a two-tier LRU cache to avoid this:

- **Block cache (256 KB blocks):** Handles the pattern where GeoTIFF.js makes a 1-byte "probe" read followed by a full read of the same region. Without caching, that doubles your round trips. The block cache absorbs the probe for free.
- **Tile cache (up to 5 MB):** Caches full tile responses. When you pan back to a region you've already visited, tiles come from memory instead of S3.

It also deduplicates concurrent identical S3 requests — if two rapid-fire tile requests hit the proxy at the same time for the same byte range, only one goes to S3.

Under the hood, the proxy uses `httpx.AsyncClient` with connection pooling (200 connections) so the S3 fetches are genuinely fast.

## What You Need: Tiled, Pyramidal OME-TIFFs

The proxy handles the URL refresh problem, but interactive viewing still requires the image file itself to be in the right format. Specifically, the OME-TIFF needs to be **tiled** (512×512 tiles) and **pyramidal** (multiple resolution levels). Without tiling, Avivator has to download entire image planes to display any region — which defeats the purpose.

You can check whether your file is tiled:

```python
import tifffile
with tifffile.TiffFile('your_file.ome.tiff') as tif:
    for i, level in enumerate(tif.series[0].levels):
        page = level.pages[0]
        tile = (getattr(page, 'tilelength', 0), getattr(page, 'tilewidth', 0))
        print(f'Level {i}: {level.shape}  tile={tile}')
```

If `tile=(0, 0)`, the file needs to be re-converted before interactive viewing will work well.

For large files (over 1 GB), there's another optimization: an `.offsets.json` sidecar file that pre-computes IFD (Image File Directory) locations so the viewer doesn't have to walk the entire IFD chain on initial load. The `generate_offsets.py` script in the repo handles this if you have a local copy of the file.

## Try It

Install from GitHub:

```bash
pip install git+https://github.com/Sage-Bionetworks/synapse-avivator.git
```

Or run directly with `uvx` if you don't want to install:

```bash
uvx --from git+https://github.com/Sage-Bionetworks/synapse-avivator.git synapse-avivator syn74326609
```

The demo file (`syn74326609`) is an 857 MB, 8-channel LuCa-7color tissue image — tiled, pyramidal, ready to stream. It's in the HTAN project `syn74326599`.

## What's Next

A few things on the roadmap:

- **Gen3/DRS support:** HTAN data lives in both Synapse and the NCI CRDC. Extending the proxy to handle Data Repository Service URLs would let it work across both sources.
- **Multi-file sessions:** Right now each run is one file. Supporting multiple files in a single session — useful when you want to compare channels from related samples — is a natural next step.
- **Packaging:** Proper PyPI release so you don't need the `git+` install URL.

If you hit a bug or want to contribute, the repo is at [github.com/Sage-Bionetworks/synapse-avivator](https://github.com/Sage-Bionetworks/synapse-avivator). Issues and PRs are welcome. If you're working with large imaging datasets in Synapse and running into problems this doesn't solve yet, open an issue — we'd rather know.
