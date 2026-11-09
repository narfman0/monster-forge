# Third-party licences

| asset | used for | licence | copy |
|---|---|---|---|
| Quaternius Universal Animation Library (UAL1, Standard) | the biped skeleton and clips (`tools/rig_ual.py`, `out/<source>/_shared/ual/`) | CC0 1.0 | `quaternius-ual1-License.txt` (from the downloaded zip) |
| Quaternius Universal Animation Library 2 (Standard) | extra biped clips | CC0 1.0 | `quaternius-ual2-License.txt` (from the downloaded zip) |
| Mesh2Motion assets (fox, horse, dragon rigs and animations; github.com/Mesh2Motion/mesh2motion-assets @ dc2eb3cf) | the quadruped mocap clips (`tools/anim_quad.py`, sampled into `data/quadmocap/`) | CC0 1.0 (repo LICENSE) | `mesh2motion-assets-LICENSE.txt` |
| Mesh2Motion app (github.com/scottpetrovic/mesh2motion-app @ fd9fdeb2): the same assets as compressed GLBs | checked only (the app's art is CC0, its code MIT) | CC0 1.0 (art) | `mesh2motion-app-LICENSE-CC0.md` |

The licence files are the ones shipped inside the zips we downloaded (2026-11-06); `quaternius-ual-zips.sha256`
pins those zips. The Mesh2Motion files (2026-11-08) are the repos' own licence files; `mesh2motion-blends.sha256` pins
the 24 .blend files sampled (`rigs/rig-<rig>.blend` and `rigs/<rig>/animation-<rig>-<clip>.blend`). Mesh2Motion's
character "variations" carry per-file licences (some CC-BY / CC-SA); none of them is used. Quaternius published a separate "Quaternius Asset License" (QAL v1.0, 2026-08-28) for newer
packs that forbids redistributing the assets; check each pack's own licence file before adding one.
