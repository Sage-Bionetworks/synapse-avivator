Hey #engineering

Built a small thing that might be useful for folks working with multiplexed imaging data on Synapse.

**`synapse-avivator`** — one command to open a Synapse-hosted OME-TIFF in Avivator, no downloading required:

```
pip install git+https://github.com/Sage-Bionetworks/synapse-avivator.git
synapse-avivator syn74326609
```

The problem it solves: Synapse presigned URLs expire after 15 minutes, but Avivator hammers the server with byte-range requests over a whole session. URLs go stale, you get 403s, viewer breaks. This runs a local proxy that refreshes URLs transparently — validated in a live session where the URL actually expired mid-view and the viewer never noticed.

A few other things it does:
- Two-tier tile cache, so revisiting a viewport is instant
- Works with any tiled pyramidal OME-TIFF on Synapse
- Uses your existing `~/.synapseConfig` auth, nothing new to set up

The demo file (`syn74326609`) is an 857MB 7-color LuCa dataset in `syn74326599` if you want something real to throw at it.

Repo: https://github.com/Sage-Bionetworks/synapse-avivator

Built with HTAN multiplexed imaging in mind but should work for anything tiled on Synapse. Happy to help if anyone runs into issues.
