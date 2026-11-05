# TODO (checkpoint 2026-11-04, rig + walk on all 6 pilots)

Decision: take the 6 pilots (goblin-warrior, wolf, owlbear, giant-spider, harpy,
young-red-dragon) all the way to rigged + animated Godot-ready GLBs **before** the
330-monster SRD batch. Batch is gated on all 6 loading and playing clips in godot-dnd.

## State

- All 6 pilots have paint + LODs (viewer: paint 6, lod 6). Sheets in `out/srd-5.2.1/_paint_cmp/`.
  Paint is 150-190 s per monster, peak 13.9 GB VRAM and ~25 GB system RAM.
- SkinTokens runs: ~20-30 s per monster, ~14 GB VRAM (stop ComfyUI first). `scipy` was an
  undeclared dependency, now installed in its venv. No sm_120 or transformers 5 problems.
- `paint.enabled` now defaults to true: it adds ~3 min per monster, ~14-17 h over 330.
- `forge.py rig` and `forge.py anim` work on all 6 (viewer: rig 6, anim 6). Each monster's
  skeleton is `rig/chosen.glb`: `--force` rebuilds the game files from it, only
  `--rig-reroll` rolls again. Art is git-ignored and `qa --force` deletes `rig/`, so the
  harpy's hand-built wing skeleton would need redoing: re-add the chains, skin with
  `--use_skeleton`, run `tools/blender_wing_weights.py`.
- ComfyUI is a user systemd unit: `systemctl --user start|stop comfyui`. Do not
  nohup it from a shell.
- Viewer: `python3 -I tools/viewer.py` -> http://127.0.0.1:8787/

- **Unarmed references: candidate check in the ref stage** (2026-11-04, B). The empty-hands
  prompt held in only 2 of 4 goblin candidates (a bow, a dagger in the fist). The ref stage
  now scores every candidate (DWPose hand crops + SigLIP) and picks the first empty-handed
  one; batch runs of the 79 `unarmed_reference` monsters should use `--n 4`.
- **ComfyUI memory ceiling.** Paint pushes ComfyUI to ~17 GB RSS; with `MemoryHigh=16G` in
  `comfyui.service.d/memory.conf` and swap full it stalls and stops answering `/queue`
  (twice on 2026-11-04). Either raise MemoryHigh to ~20G or restart the unit before paint.

## Rig findings (2026-11-04)

Generation is sampled and unseeded (`do_sample=True`), so every run gives a different
skeleton. Re-rolling is cheap; a stage needs N rolls, a scorer and retry on failure.

| pilot | best rig | result |
|---|---|---|
| goblin-warrior | `rig/model_rigged.glb` (30 joints) | good; pose test clean, weapons follow the hands |
| wolf | `rig/model_rigged.glb` (28 joints) | good skeleton; no real tail chain, pose test not run |
| owlbear | `rig/roll_3.glb` (27 joints) | good; pose test clean. No spine, legs hang off a back root. Bone counts varied 18-46 across rolls |
| giant-spider | `rig/model_rigged.glb` (37 joints) | 8 legs, pose test not run. First run died with `assert res.skin_pred is not None`, the retry passed |
| harpy | `rig/wings/model_fixed.glb` (34 joints) | no wing bones in 4 of 4 rolls; uses hand-added chains and fixed weights (below) |
| young-red-dragon | `rig/model_rigged.glb` (67 joints, roll 1) | best rig: spine, neck, 4 legs, tail, both wings. Clean at +/-25 degree flap, neck, leg and tail bends |

- Manifold cleanup is **not** needed for rigging: the loose parts are slivers of mostly 3-9
  verts on one main body, and they weight fine. Only the dragon has a large one (332 verts).
- `--use_skeleton` re-skins hand-edited skeletons, but its `trim` augment deletes joints
  with no weights: give new bones placeholder weights in the input GLB.
- Harpy wings: SkinTokens spreads the wing vertices over forearm, thigh and spine even with
  a wing chain, so the wings tear. `tools/blender_wing_weights.py` re-weights them
  geometrically (`rig/wings/model_fixed.glb`). Clean up to about +/-20 degrees; a steeper
  downstroke stretches the inner wing and drags a long feather tip.
- **Facing matters to SkinTokens.** The re-rolled dragon went from scrambled to clean once
  its head pointed forward. QA's PCA auto-facing followed the wings and left it 65 degrees
  off, so it now has a pinned `yaw`. The batch needs a facing check before rigging.
  Owlbear's skeleton has no spine and it looks a little off-axis from above: re-check its
  yaw. New prompt in `data/prompt_tweaks.json`; old mesh in `young-red-dragon_exp_curled/`.

## Next (in order)

### B. Pilot hardening
- [x] Write `docs/paint-stage.md` (forge.py/paint.py already reference it). Rig and anim: `docs/rig-anim.md`.
- [x] Forward axis: keep -Z (`forge.py` default). Matches Godot node forward; leave
      `use_model_front` off in `look_at()`. Check UniMate's expectation in step E.
- [ ] Any `qa --force` deletes paint and LODs; `paint` deletes LODs. Use `--stages paint,lod`.
- [x] (2026-11-04) Unarmed-candidate check (`tools/unarmed_check.py`, `docs/references.md`):
      after the ref stage draws its candidates, each `unarmed_reference` monster's hands are
      found with DWPose (ComfyUI's controlnet_aux weights) and scored with zero-shot SigLIP
      so400m ("a hand holding <weapon>" vs empty-hand prompts; downloaded, 3.5 GB,
      Apache-2.0); > 2.5 = armed; the first unarmed candidate is picked unless `ref_pick` is
      pinned; `manifest.ref.unarmed_scores` / `ref.unarmed`, a warning when all are armed.
      Override `ref_unarmed_check` (default on). 20 of 20 right on goblin-warrior (bow 3.35,
      dagger 3.34, empty -1.54/-0.82), bandit, guard, knight (two swords 7.87) and gladiator;
      empty hands reach 1.90, so the margin is thin. The goblin's pinned `ref_pick` 2 is kept
      (its ref.png is unchanged). bandit, guard, knight and gladiator now have 4 refs each.

### C. M3 Rig
- [x] `forge.py rig`: N rolls, retry, score, keep the winner, rigged LODs, manifest `rig` key.
- [ ] Add wing chains and wing weights to the stage (harpy is by hand; other winged bipeds will
      hit the same gap). The dragon's came from SkinTokens.
- [x] (2026-11-04) `rig_reweight` override (`tools/rig_reweight.py`, applied in the finalize
      path before sockets, so `rig --force` keeps it): geometric re-weight of a chain
      ({bones, reach, bias, blend, strip, fit, hand_room, far}), optionally fitting its
      joints into the limb. Harpy right arm (`[bone_18, bone_19]`, fit): see E. The wing
      re-weight (`blender_wing_weights.py`) could become a `rig_reweight` entry too (not done).
- [x] Facing check before rigging (see Rig findings). (2026-11-04) Mirror-plane body axis +
      head-end cue on `model.glb`, `manifest.rig.facing`; refuses to roll past 30 degrees at
      confidence >= 0.5 (`rig_facing_check` override to skip). All 6 pilots pass (0-6.5
      degrees); the dragon turned back to QA's auto-yaw error measures 58.5 and is refused;
      the old curled dragon warns (15 degrees, confidence 0.38, twisted pose). `docs/rig-anim.md`.
      Viewer: `viewer/app.js` FIELD_UI needs `rig_facing_check: { type: 'choice', opts: [true, false] }`
      (it renders as a text box until then), and a HUD for the index's `rig_quality`.
- [x] Roll score v2 (2026-11-04): spine, head, tail, wing and arm chains per plan, leg count
      and pairs, joint band, joints outside the mesh, weight bleed, and a pose test (each
      part bent 30 degrees: torn edges, far vertices moving, skin not following). Terms in
      `rig/roll_<i>.json`, `manifest.rig.rolls`, `manifest.rig.quality`, index `rig_quality`.
      On the kept rolls it picks the goblin's and the dragon's current rigs (v1 would have
      taken dragon roll_2) and owlbear roll_1 over the kept roll_3 (verifier decides).
      The 22-vs-28-joint wolf rolls are no longer on disk, so that case is untested.
- [ ] Weights-heatmap thumb per roll (the score's overlay sheets are a scratch script so far).
- [ ] `find_legs` misses feet that fan into toes from a high ankle (owlbear roll_2): the
      score then counts missing legs and the anim stage would fail on that roll.
- [ ] Owlbear: no roll on disk has a spine (all four hang the legs off one root); re-roll,
      the facing is fine (4 degrees). Wolf: no tail chain (its only roll).
- [x] (2026-11-04) `find_arms`: chains off or through the head (hair, the neck) and hanging
      chains that reach the ground band (a tail) are not arms; a side with no hanging arm
      takes a raised chain that reaches less than `WING_REACH` x the height (less than a
      wing). Merfolk: both arms, `hand_r`/`grip_r` placed, `back` on the chest; the 6 pilots,
      the ghost and the eagle keep the same arms, head, wings, tail and sockets (finders run
      on every pilot's chosen and rigged skeleton, `rig --force` on the 6 + ghost: sockets
      and quality scores identical). `find_head` was right on the merfolk (bone_5 owns the
      skull); the hair chain had been its left arm.
- [x] (2026-11-04) `manifest.errors`: `rig` and `anim` drop their stage's entries when they
      succeed (`rig.clear_stage_errors`). Other stages still keep theirs (the goblin's two
      paint timeouts): a generic clear belongs in forge.py's stage loop.
- [ ] Rigify landmark-fit fallback for biped/quadruped if SkinTokens output is bad.

### D. M4 Animate (core set: idle, move, attack, hit, death)
- [x] Procedural in-place `walk` for all legged plans (`tools/blender_clips.py`, planted IK feet).
      Loose parts below the hips no longer dip: the skinned mesh is measured every frame
      and the body raised (or a ground-lying tail turned up) to keep every vertex above
      z = -0.03 m. IK now holds the foot bone itself (position + rest orientation): SkinTokens
      feet can hang 0.7 m off the shin's tail (dragon), which made them bob.
- [x] Remaining clips (2026-11-04): idle, attack_melee, hit, death for all 6; `fly` for harpy
      and dragon (wing chains found by `rig_topology.find_wings`), `breath` for the dragon,
      `attack_ranged` (bow draw) for the goblin. All 6 pilots `anim_complete` in the viewer.
      Shape and per-clip numbers in `viewer/README.md` ("Clips the anim stage writes").
      Known: owlbear walk and lunge run at half size (`stride_scale` 0.5): not the tail fan
      (on the tail chain in the kept roll_1; on a hind shin only in the old roll_3) but the
      front paws, whose non-sole vertices dip 5-14 cm at full stride with the standing feet
      0.093 m short, so a fan re-weight (`rig_reweight`) was tried on paper and not added;
      harpy walk also half stride (hip feathers skinned to the root and right thigh touch
      the ground at rest, no tail chain to turn, so the body cannot crouch); harpy wings are clean only to
      about +/-20 degrees, its arms are mostly hidden by the wings in the sword cut.
      A library retarget would still need a bone map, since SkinTokens bones are `bone_N`.
- [x] (2026-11-04) Ground-resting tail fans: `find_tail(ground=True)` takes a 2+ bone chain
      that slopes from the hips into the ground band when no horizontal tail exists, and the
      floor clips turn it up (with a settle-back for such fans) instead of raising the body.
      Giant eagle walk (scratch): min_z -0.085 -> +0.001 m, body raise 0.092 -> 0, stride
      0.5 -> 0.8; its idle, lunge (now full size) and hit no longer raise the body. All other
      pilots' clips unchanged (clip and frame counts, min_z, raises).
- [x] (2026-11-04, anim3) Round 4 fixes (E): ghost train folds flat in death and lift clips
      raise frames that dip, the fungus falls from a planted root, the merfolk's top arm lies
      down, the swarm scatters and sinks, lifted idle for swim/fly-only monsters, the eagle's
      head is its beak (`find_snout` skips wings) and it stands on its feet (rig
      `ground_feet`, H). All 15 pilots re-animated: identical clip and frame counts; only
      ghost, giant-eagle, violet-fungus, giant-octopus and merfolk change numbers (gate in
      the anim3 report).
- [ ] UniMate text-to-motion for attacks (own uv venv under tools/).
- [x] Bake to actions -> one armature-only `anims/<clip>.glb` per clip (viewer/README.md).

### E. Godot import test
- [x] godot-dnd has no 3D scenes yet (Godot 4.7): add a test scene first.
      (2026-11-04) godot-dnd now has `tools/sync_monsters.py`, which copies
      `out/<source>/<id>/` into `assets/monsters/`, plus `scenes/monster_lab.tscn`,
      `tools/check_monsters.gd` (headless import test) and `docs/monsters.md`.
- [ ] Load each pilot: AnimationPlayer with the 5 clips, LOD switch. All six pilots load and
      play walk, and the LOD switch works. Re-run when the other clips land:
      `python3 -I tools/sync_monsters.py --all && godot --headless -s res://tools/check_monsters.gd`
      (in godot-dnd). Findings from Godot 4.7.1:
  - Holds: metres, feet at y=0, +Y up, faces **-Z** (all six; `look_at` works with
    `use_model_front=false`), creature's right = +X, `scale_m` = longest body axis.
    Rigged LODs share bone names, order, bind poses and skin with LOD0. Clip tracks
    bind 100% by bone name. Walk clips are in place.
  - Socket != grip: a `BoneAttachment3D` on `hand_r` puts the prop's grip at the wrist,
    with +Y along wrist -> fingers. On the goblin the sword hangs down along the fingers.
    `equipment.md` needs a grip frame: `grip_r`/`grip_l` bones in prop space (origin at
    the fist centre, +Y across the palm), or a documented hand -> grip offset.
  - The harpy's `hand_*` are added bones at the hand bone's tail, 8-9 cm off the mesh,
    while the goblin's are the renamed hand bones (at the wrist). Harpy rig: the skeleton
    is offset +6.8 cm in x, and bone_18/bone_19 (right forearm/hand) have no vertex with
    weight > 0.5, because the right arm is skinned to the wing chain bone_11/bone_12.
  - Walk dips below the ground: lowest vertex over a cycle is owlbear -0.19 m (bone_25),
    dragon -0.10 (tail bone_64), harpy -0.10 (bone_0), wolf -0.05 (foot bone_26).
  - The rigged LOD files' material is named `Material_0.001`, not `Material_0` as lods.md
    says. Harmless in Godot, which applies LOD0's material.
  - Goblin 16:02 build: `textures/base_color_painted.png` is about 60% black (mean RGB
    39/32/20, `paint.painted_fraction` 0.39). Texels no view painted look unfilled
    instead of falling back to the voxel colour, so the goblin renders almost black. The
    15:47 build looked right.
- [x] Round 2 (2026-11-04): all 36 clips bind 100% of tracks in Godot 4.7.1, LODs follow
      the one skeleton, one-shots settle to idle and death holds; props sit in the fist on
      `grip_r`/`grip_l` through the sword cut. godot-dnd commits 8dc462a, 0a13bc6
      (`tools/sync_monsters.py`, `scripts/monster.gd`, `scenes/monster_lab.tscn`,
      `tools/check_monsters.gd`, `docs/monsters.md`).
- [x] Metallic: the Pixal3D metallic bake is noise (255 on goblin and harpy, 0 on wolf), so
      the goblin read as full metal in Godot. QA and rig exports now clamp metallic to 0.
- [x] Harpy: `grip_r` was 12-27 cm from its hand because the right arm was skinned to the
      wing chains (the left wing's bone_12/bone_13 too) and bone_18/bone_19's weights sat on
      the right thigh. (2026-11-04) Fixed with the `rig_reweight` override (C), no re-skin
      or re-roll: `hand_r`/`grip_r` 1.3/0.2 cm from the skin (5.5/8.0), 3.3/3.0 cm from the
      fist centre (10/12); bone_18/bone_19 own 197/108 vertices (0/0). Sword cut: the arm
      and sword move together and the thigh no longer follows the arm (edges stretched past
      4x 175 -> 77); fly: wings as before (past 4x 211 -> 223, the arm now stays down instead
      of flying into the wing). Clip and frame counts unchanged. Checked in Blender sheets
      only: Godot not re-synced. The "+6.8 cm off centre" is the bbox (asymmetric wings):
      the torso's own centre is x 0.01-0.03, so the spine sits ~4-5 cm right; not changed.
- [ ] Bows and shields need a per-prop carry pose: at rest a bow on `grip_l` lies on its
      side. Options: a `carry` offset per prop in props.json, or stow it on `back`/`hip`
      outside `attack_ranged`. The lab also keeps the sword out during the shot: loadout
      swap per clip is the game's job.
- [ ] Real export (`assets/monsters/*` in the export filter) is untested.
- [x] Round 4 (2026-11-04, godot-dnd uncommitted at review time): the nine body-plan pilots
      (centaur-trooper skipped, no clips). `check_monsters.gd` ALL PASS on 15 monsters. 81
      clips bind 100% of their tracks. Every GLB channel that leaves rest has a Godot track,
      scale included, and the blob/swarm/octopus scale keys play. 4 LODs share one skeleton
      per rig, swarm included. Loop seams are 0, and every death ends at lowest y 0.000. No
      clip goes below -0.03 m. godot-dnd: `Monster.move_clip()` (walk, else swim/fly),
      `blend_time` cross-fade (lab 0.25 s), sync `--all` needs clips, ground/channel/
      transition probes in the check. Pipeline findings, open:
  - [x] ghost death: the back of the robe stays upright as a ~0.9 m vertical sheet while
        the body lies flat (the hem's weights go to bones the fall does not carry). The hit
        clip dips the hem to -0.010 m, despite `lift`.
        (2026-11-04, anim3) Not the weights: the train lies flat on the floor behind the
        feet at rest, square to the body, so the fall stood it up. The hem's first bone
        below 0.15 x the height (`bone_28`, `bone_32`) now folds back against the fall:
        last frame top 1.28 -> 0.78 m, body raise 0.30 -> 0.18 m. `lift` clips raise a frame
        that dips: hit lowest vertex -0.013 -> 0.000 (lift up to 0.193 m). No re-weight step.
  - [x] merfolk-skirmisher: no `hand_r`/`grip_r` (raised arm missed), so the registry spear
        is never shown. attack_melee barely moves the spread arms. Death leaves the raised
        arm pointing straight up.
        (2026-11-04, anim3) Both arms since df2a463 (`hand_r` bone_28, `grip_r`): the cut
        winds the right arm overhead and down across the body (sheet). Death: the right arm
        was found now and swung up, but the left arm, held out sideways at rest, pointed at
        the sky once she lay on her right side; it now drops against the body (hand 0.76 ->
        0.65 m, along the torso). The tail fin still stands up at the end (0.83 m).
  - [x] swarm-of-rats mesh is a 1.44 m column of rats (bbox 0.83 x 1.44 x 1.50), not a
        spread swarm. The death y-scale 0.15 squashes the rats flat.
        (2026-11-04, anim3) Death is "scatter and sink": 1.7x in X and Y, 0.3 of the height,
        a 12 degree slew. The mesh stays (no GPU); the batch models one unit per swarm (H).
  - [x] violet-fungus death topples the root clump with the stalk, so the base stands up
        vertically.
        (2026-11-04, anim3) The fallback weights were right (the root bone owns 100% of the
        base up to 0.2 m); the clip tipped the root. Now the root stays and the stalk breaks
        over at `spine_01`/`spine_02` by the angle that rests the cap on the ground
        (bisected, 83.6 degrees). No re-rig.
  - [ ] Height changes between clips are by design but pop without a blend: octopus idle on
        the floor vs swim at 0.215 m, merfolk idle vs swim at 0.65 m, eagle idle vs fly at
        0.41 m. Idle -> move in one frame moves vertices 0.14-1.31 m (a 0.25 s cross-fade
        brings this down to 0.03-0.26 m). Decide whether to add a take-off clip or a lifted idle.
        (2026-11-04, anim3) [x] Lifted idle: `anim.idle_lift` / override `idle_lift`. No
        `walk` in the animation_set -> the idle is held where swim/fly holds the body
        (`match_move`); walkers keep a floor idle (harpy, dragon). Octopus idle lift 0.062 ->
        0.341 m (= swim), merfolk 0 -> 0.18 (= swim), eagle hovers at 0.32 like fly (a slow
        small-beat fly loop). The merfolk's swim still pitches the body 55 degrees, so its
        idle -> swim change is a turn, not a height pop. Re-check in Godot.
  - [ ] Note: hunter-shark rest mesh is strongly curved (2.97 m tall for 4.5 m long). It
        swims level at 0.74 m and death lies belly-up on its dorsal fin.

### F. Props (2026-11-04)
- [x] `tools/props.py` + `tools/blender_prop_qa.py`: all 32 props in `out/props/<id>/` (prop
      space, LOD0 at `tri_budget`, LOD1-3, axes sheet, manifest `prop` key); every exported
      GLB passes the length / origin / -Z check. Sheet: `out/props/_contact.png`.
      Docs: `docs/equipment.md` "Generating props". Viewer: `?prop_r=<id>&prop_l=<id>`.
- [x] Real longsword + longbow on the goblin-warrior's `grip_r` / `grip_l`: handle in the
      fist, blade forward; the bow first needed a +90 deg X turn, now identity (the stave runs
      through the fist; `attack_ranged` raises it). Rechecked 2026-11-04 with the `string` node.
- [x] Weak refs rerolled (2026-11-04, up to 2 rounds, `data/overrides/props/<id>.json` `notes`
      has every round). On-model now: glaive (curved single-edged blade on a pole; the
      blade is ~30% of the length, `edge_rule: convex`), harpoon (barbed arrowhead, rope at
      the butt, `roll_deg: 90`), greatclub (bat-shaped club, `flip_tip`). Better but still
      off-model after 2 rounds, not retried: fork (a trident-like head, centre spike plus two
      hooked side prongs, not two parallel tines; round 2 drew paired spears), heavy crossbow
      (side-on, solid stock, prod at -Z, conf 0.98, but a modern telescopic sight and a
      rifle-shaped stock; round 2 drew ring prods and scopes again). Mace: both rounds were
      worse (a stick with a tiny cap; a star, a stick, sword hilts), so the first roll is
      kept (big flanged disc head with a spike on top: passable, off-model in detail).
- [x] Whip: procedural (`"procedural": "whip"`, `tools/blender_prop_procedural.py`):
      handle with pommel knob and ferrule knot, braided lash tapering to a popper, 20 deg
      droop toward -Z, 992 tris, 2.5 m along +Y (arc 2.53 m), `prop.source = "procedural"`;
      same QA (prebuilt frame), check and LODs. Still no spline-IK bone chain (`rig` in
      props.json).
- [x] Low orientation confidence, checked on the axes sheets (2026-11-04): shortsword 0.05
      ok (blade +Y, guard across Z, grip on the handle); quarterstaff 0.22 ok (symmetric
      staff, grip 0.35 L); javelin 0.36 ok (point +Y); warhammer 0.41 wrong edge (the hook
      was at -Z): `flip_edge`, the squared face is at -Z now; pistol 0.48 barrel climbed
      ~25 deg: new override `pitch_deg` (-25), barrel level, muzzle -Z; glaive 0.3 -> 0.49
      (new ref, `edge_rule: convex`); harpoon 0.12 -> 1.0, heavy crossbow 0.33 -> 0.98 (new
      refs); greatclub 0.3 (new ref, upside down by head_end: `flip_tip`, verified).
- [x] Bows: the string is its own mesh node `string` (straight, 4 sides, 3 mm, 8 tris,
      material `bowstring`) next to the stave node `prop`, in model.glb and every LOD
      (`blender_lods.py` `keep`); `grip_offset` stays identity. LOD0 (the stave) is still
      under budget (shortbow 397 / longbow 692 of 800); a bone chain for the draw is open.
- [x] forge.py comfy-fallback decimation: up to 5 DecimateMesh runs, bisecting between an
      under and an over target, keeping the run closest to the budget from below
      (`mesh.decimate_kept`). The old loop also ran a 4th, unlogged retarget and kept it
      (first greatclub: logged 189 / 2126 / 182, kept 2765 tris). Test: the first greatclub's
      sawn-log ref with `--decimator comfy`: 184 / 2220 / 641 faces, kept 641 of 700.
- [ ] LOD check: every LOD0 passes, but spear LOD1-3 fail the -Z test (its round point is
      as wide in X as in Z: 0.067 / 0.072 m at LOD0, decimation flips it) and shield LOD1
      measures 0.822 of 0.8 m (2.8%, QEM grows the rim). Untouched since the first run.
- [ ] Godot: `BoneAttachment3D` on `grip_r`/`grip_l` + `prop.grip_offset`; off-hand IK to
      the `offhand_grip` node of two-handed props; stowed props on `back`/`hip_*` need
      their own per-category offsets (not measured).
- [ ] Variants (`props.json` `variants`: Flame Sword, Bone Bow, ...) as separate props.

## Later
- **Public SRD viewer (static GitHub Pages repo, e.g. `monster-forge-viewer`).** Gated on
  the local iteration above: only "released" monsters go out.
  - [x] Define the release gate (manifest flag, e.g. `released: true`, set once a monster
        is through rig + anim + Godot import). (2026-11-04: `tools/release.py <id>... [--unset]`;
        set on the six pilots.)
  - [x] `tools/publish.py`: walk `out/srd-5.2.1/`, copy released monsters (LOD GLBs,
        preview image, `anims/`), bake `index.json` with the same logic as
        `tools/viewer.py` `/api/index.json`, write to the viewer repo. (2026-11-04: also the
        wielded props + `props.json`, viewer files, README with the attribution; `--clean`.)
  - [x] Viewer `static` mode in `viewer/app.js`: read `./index.json` and relative model
        paths; hide overrides form, re-run queue and progress dashboard; keep model view,
        wireframe/texture, LOD picker, animation playback and `#<id>` deep links. (2026-11-04:
        detected by a meta marker / `?static=1` / no `/api/`; the monster's own props by default;
        checked in Chromium: six pilots, all clips and LODs, no `/api/` or `/out/` requests.)
  - [x] Public extras: search/filter by type, CR, size from the registry; CC-BY-4.0 SRD
        attribution footer (text in README).
  - [x] Measure real per-monster size before choosing hosting. Pages limits: ~1 GB site,
        100 MB per file, no Git LFS. If too big: meshopt + KTX2, only LOD1/LOD2, or host
        models in a bucket (R2) and keep only the viewer on Pages. (2026-11-04: six pilots =
        87 MB; 5.6-16.4 MB per monster, LOD0 with embedded 2K textures is 3.8-14.5 MB of it,
        largest file 14.5 MB. 330 at ~15 MB = ~5 GB: over 1 GB, so the full SRD needs KTX2 /
        smaller textures or a bucket; ~60 monsters fit as is.)
  - [ ] Create the Pages repo (`monster-forge-viewer`): `git init` in the publish dest,
        push, enable Pages on the default branch root; re-run `publish.py` + commit to update.
  - [ ] Check generation-model terms (Pixal3D, SDXL, ControlNet, IP-Adapter weights) allow
        publishing outputs before the repo goes public.
  - Start with the two painted pilots (harpy, goblin-warrior) as test data.
- [x] `tools/import_names.py`: 5etools `bestiary-xmm.json` (~/workspace/5etools-src, local use
  only, never vendored) -> `data/registry/mm-2024.json` stubs. 503 blocks, 330 flagged
  `srd52`, 173 MM-only. Also cross-check SRD registry size/type/speed against it.
  (2026-11-04: 173 records, all `rule`; `--check-srd` found 8 differences, see below.)
- M2 batch SRD once the pilots are through E.

### G. MM-2024 (personal use only; `data/registry/mm-2024.json`)
- [ ] Body-plan review: 129 of 173 are rule guesses at 0.5 (128 fall to `biped`). Needs
      `mm-2024` keys in the curation files: `body_plan_overrides.json` is keyed by bare slug
      and `build_registry.py` rejects unknown slugs, so it needs a per-source layout first.
- [ ] Equipment review: 152 records need a `data/equipment.json` `mm-2024` entry (hands
      default on for every rule `biped`). Prop specs missing for `maul`, `flail`, `trident`,
      `lance` (dao, elemental-cultist, gnoll-fang-of-yeenoghu, lizardfolk-sovereign,
      mezzoloth, sahuagin-baron, salamander-inferno-master, thri-kreen-psion).
- [ ] Hand-written `visual_profiles.json` descriptions, at least for the non-humanoid
      shapes the A-pose template draws wrong (beholder, spectator, carrion crawler,
      displacer beast, grell, intellect devourer, ...). kuo-toa-whip: "whip" in the name of
      an empty-hands reference.
- [ ] Hand `scale_m` (all 173 are size-category defaults).
- [ ] Rigs MM needs that the pilots do not cover: floating (beholder family), tentacled,
      merfolk_naga (yuan-ti), centauroid, serpentine, amorphous, plant_object, swarm.
- [ ] SRD cross-check (report only, not applied): clay-golem walk 30 in the SRD registry vs
      20 in 5etools; lycanthropes walk 40 (beast form) vs 30 (humanoid form);
      swarm-of-insects has a climb-or-fly 20 choice the SRD registry drops. Re-check
      against the SRD 5.2.1 PDF.

### H. Body plans (2026-11-04)
One pilot per uncovered plan through the whole chain. Strategies, numbers and the plan table:
`docs/rig-anim.md` "Body plans". Sheets (side, 3/4, top x 4 frames per clip, rig overlays) were
scratch files. Per-plan settings: `rig.RIG_STRATEGY`, `anim.LEGLESS`/`ARMED`,
`forge.MAX_SCALE_PLANS`, `rig.FACING_ADVISORY`/`HEADLESS`/`UPRIGHT_BODY`.
- [x] serpentine (constrictor-snake): SkinTokens gives a 7-13 joint star (4 of 4), so
      `fallback:spine_chain` (16 bones along the geodesic centreline) + slither/strike clips.
      Batch: fallback for all 11. **The reference is the problem**: juggernaut never drew a
      stretched-out side-view snake in 5 prompt rounds (it draws top-down coils, which mesh as an
      upright cutout); the pilot uses a ground-lying S seen from above + `level` 60 degrees.
      Coils that touch fuse the surface: weights are fixed by a nearest-bone pass, but expect
      some bunching in the slither. Needs a pose ControlNet / better model for snakes.
- [x] floating (ghost): SkinTokens rigs the robed ghost as a biped under the robe (arms, hands,
      spine, head). Hover clips at a constant 0.1 x height, death falls onto the ground. Batch:
      SkinTokens. The registry prompt draws a legged zombie: prompt tweak (legless tapering
      robe) needed for ghost/specter/wraith. Elementals (column + arms) untested.
- [x] avian (giant-eagle): legged framework as is; SkinTokens found both wings when the
      reference spreads them sideways (front view, ref 1). The side-view ref with wings raised
      back gave one wing that was also taken as the tail. No roll had a spine. Batch: prompt
      "wings spread wide to the sides"; theropods (`custom.theropod`) are wingless. The tail
      fan resting on the ground is now turned up (walk dip 8.5 cm -> none, D). (anim3) Fixed:
      the rig stage stands it on its feet (`blender_rig.ground_feet`: lowered 0.355 m, fan
      turned up 33.9 degrees, baked into LOD0-3, `manifest.rig.grounded`), and `find_snout`
      skips wing chains (head `bone_11` -> `bone_5`, the beak). Its mesh is 29.5 degrees off
      +Y (just under the facing check's 30): the wings lie on a diagonal; a pinned `yaw` and
      a re-run of qa/paint/lod would fix it (GPU).
- [x] amorphous (black-pudding): `fallback:vertical` (4 bones) with squash-and-stretch bone
      scales. Batch: fallback. Ref pick matters: drips under a raised blob meshed as a spider.
      The lemure (`blob_torso`, arms) will want SkinTokens + arms instead.
- [x] plant_object (violet-fungus): `fallback:vertical` (5 bones); SkinTokens rooted the fungus
      at its cap. Batch: fallback for fungi and shrubs; trees with branches, the rug
      (`cloth_grid`), the flying sword (`fly`, rigid prop) and the troll limb need their own
      look: the clip code swings limbs off the stalk when the skeleton has them.
- [x] tentacled (giant-octopus): `fallback:tentacle_hub` found 8 tentacles; SkinTokens rolls also
      gave a hub (unnamed). Batch: fallback. Reference was a top view (meshes as an upright
      splayed cutout, still rigs). Otyugh/roper (legs or a column) untested.
- [x] aquatic (hunter-shark): SkinTokens is clean (spine, fins, tail lobes); carangiform swim
      held 0.25 x height up, death rolls belly-up onto the ground. Batch: SkinTokens; flippered
      plesiosaur/archelon untested.
- [ ] centauroid (centaur-trooper): **no centaur mesh**: juggernaut drew a man riding a horse in
      8 of 8 over three prompts. A rider proxy (prancing, one hoof up) rigged with both arms,
      head and spine; the stage refuses its 3 grounded feet. In scratch with feet 3 all six clips
      build (sword cut and bow draw on the arms). Batch: blocked on references (Z-Image /
      FLUX.2-klein or a pose ControlNet); a four-hooves-down stance prompt.
- [x] merfolk_naga (merfolk-skirmisher): SkinTokens (spine, tail, arms); legless builders with the
      biped sword cut, tail kick swim. Both arms found since the raised-arm fallback (C), all
      sockets placed. Batch: SkinTokens; the marilith's 6 arms untested.
- [x] swarm (swarm-of-rats): `fallback:root` + root-only clips. The reference draws a wall of
      rats (one cluster mesh); the batch should model one unit with the unit's plan and leave
      the swarm to particles. Death scatters and sinks (1.7x wide, 0.3 high) rather than
      squashing the pilot's column flat.
- [x] Fix found on the way: `blender_rig.add_sockets` renamed the head to `head` and then
      failed to parent `back` to it (KeyError), now aliased. The bone was the head after all
      (it owns the skull); `back` went there because the left "arm" was a hair chain off the
      head. Fixed in `find_arms` (C): `back` is on the chest now.
- [x] Stale `manifest.errors` entries on ghost, merfolk and snake removed (their rig stages
      succeed); rig and anim now clear their own entries on success (C).
