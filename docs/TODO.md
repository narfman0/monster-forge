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
  `--rig-reroll` rolls again. Art is git-ignored and `qa --force` deletes `rig/`; the
  harpy's wing chains are inserted by the rig stage from the mesh (no hand work since
  2026-11-04 evening: `rig/chosen.glb` is SkinTokens roll_1).
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
| harpy | `rig/chosen.glb` = roll_1 (SkinTokens, no wing bones in 4 of 4 rolls) | the rig stage inserts `wing_r/wing_l` chains from the mesh and blends the arm-wing seam (docs/rig-anim.md "Wing chains from the mesh"); the earlier hand-added chains are retired |
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
- [x] (2026-11-04) Pose templates for the ref stage (`ref_template`, docs/references.md):
      depth ControlNet templates drawn by `tools/blender_ref_templates.py`, default per plan
      (serpentine `serpentine_side`, centauroid `centaur_34`; biped/winged_biped keep the
      style's OpenPose A-pose), overridable per monster. Snake and centaur re-referenced.
- [x] (2026-11-04) Unarmed check subject: a display name with a weapon word ("kuo-toa whip")
      is replaced by the visual_profiles desc's first clause or "a creature"
      (`unarmed_check.subject`, `forge.visual_desc`); goblin scores unchanged.
- [ ] **Paint blocked by memory (2026-11-04 evening)**: ComfyUI's unit has MemoryHigh 16G /
      MemoryMax 20G / MemorySwapMax 2G, and system swap was full (8 of 8 GB, idle desktop
      apps), so the paint workflow (ComfyUI 17.3 GB) sat throttled at MemoryHigh (one view
      433 s, normally ~15 s) and was OOM-killed twice. constrictor-snake, centaur-trooper and
      giant-eagle have no paint now (qa --force removed the old one): run
      `forge.py run <id> --stages paint,lod,rig,anim --force` for each when swap has room or
      the limits allow ~18 GB (rig --force keeps rig/chosen.glb). (2026-11-05) Also unpainted
      now: giant-eagle (qa again for the avian scale rule), ghoul, giant-vulture, swarm-of-bats,
      swarm-of-insects (new meshes, `--stages` without paint).
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
      hit the same gap). The dragon's came from SkinTokens. (2026-11-04) Done in the stage:
      finalize (and the chosen-rig score) finds missing wings on the mesh and inserts
      `wing_<side>_00..03` with geometric weights (`blender_wing_weights.add_missing_wings`, a
      `rig_reweight` entry with the lobe's vertices; `manifest.rig.wings_added`), or renames a
      SkinTokens chain that falls short of `find_wings`' reach (eagle roll_1). No-op on the
      dragon, eagle, goblin, wolf, owlbear, spider (`rig --force`: sockets and scores identical).
      (2026-11-04) Arm-wing seam (`blender_wing_weights.seam_blend`): arm vs wing ownership
      follows the welded surface (geodesic from the arm's skin vs the wing's core, smooth
      falloff, one-way strips), so feathers behind the forearm leave the arm. Harpy roll_1 +
      inserted wings + seam vs the hand-built rig: `fly` 96 vs 217 edges past 4x, sword cut 48 vs
      85, `idle`/`walk` 1/5 vs 0/5, sockets <= 0.8 cm, score -0.84 vs -3.47, so the harpy's
      `rig/chosen.glb` is now roll_1 (override: no `rig_reweight`). Left: the inserted chains
      and the seam on the 20 other winged bipeds are unverified (a batch check of fly/sword-cut
      tearing per monster); `fly` still stretches the hanging wing tips skinned to the thighs
      (docs/rig-anim.md "Wing chains from the mesh").
      (2026-11-04) Body seam (`blender_wing_weights.body_seam`, after the arm-wing seam): wing
      vs body (centre line + legs) and arm vs body ownership follow the welded surface too, so
      the hip feathers beside the thighs leave the wing tip, a floor feather leaves the ankle,
      and the back and waist leave the arm. Harpy: `fly` 96 -> 4, sword cut 48 -> 14, `idle`/
      `walk` 1/5 -> 0/0, `hit`/`death` 28/40 -> 0/4, sockets unchanged, score -0.84 -> -0.42;
      `rig --force` on the goblin and dragon leaves their files byte-identical. Left: 14 edges
      on top of the right shoulder in the cut (wing root vs raised upper arm); the rule on the
      other winged bipeds is unverified.
- [x] (2026-11-04) `rig_reweight` override (`tools/rig_reweight.py`, applied in the finalize
      path before sockets, so `rig --force` keeps it): geometric re-weight of a chain
      ({bones, reach, bias, blend, strip, fit, hand_room, far}), optionally fitting its
      joints into the limb. Harpy right arm (`[bone_18, bone_19]`, fit): see E. The wing
      re-weight is now a `rig_reweight` entry with a `lobe` (inserted wing chains, above).
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
- [x] (2026-11-04) Generic clear: `forge.py`'s run loop drops a stage's `manifest.errors`
      entries when that stage ran and succeeded (its `manifest.timings` value changed; a
      skip leaves it alone, so a skipped stage keeps its errors). Tested on the eagle with
      seeded stale entries: a skipped `lod` kept both; `run --stages lod,rig,anim` cleared the
      lod entry ("lod ok, cleared its old manifest errors") and kept the paint one. The
      goblin's two paint timeouts stay until its paint really runs.
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
- [x] (2026-11-04, anim4) Round 5 fixes (E): one-shots of a lifted monster held at the idle
      height (death eases down), `match_move` matches the move clip's first-frame body,
      attack style per wielded prop category (spear thrust), arm relax for raised rest arms,
      harpy left-arm `rig_reweight`. Gate on 13 pilots (constrictor-snake has no rig at the
      moment: another stream is re-making its mesh; giant-eagle not run): identical clip and
      frame counts, numbers change only on giant-octopus and merfolk-skirmisher (lifted
      one-shots, octopus idle 0.341 -> 0.205); ghost death changes only its first 0.3 (starts
      at its idle height), harpy only its left arm.
- [x] (2026-11-04, clips2) The clips the SRD sets ask for and nothing built: `cast` (arms:
      hand_r up over the head, hold, throw forward; no arms: head/neck rear and snap; 1.2 s),
      `swim` for legged plans (lifted 0.5 x drop, diagonal paddle, wings folded; `lift`, so a
      swim-only monster idles at its height) and floating, `burrow` (dig with the front limbs,
      sink half the body height; the one clip allowed below the ground), `breath` on any head
      chain (upright: torso lean back), `fly` without wings (a levitation at `HOVER`);
      serpentine cast, breath, burrow, fly. Metre thresholds (`FLOOR_TOL`, `FOOT_TOL`,
      `DIP_MAX`, `HOVER`, fly bob) scale with the body outside the pilots' 0.95-6.5 m band
      (`SIZE_BAND`, `SCALE`). Gate on 13 pilots: every clip report identical (no pilot's set
      has a new clip). docs/rig-anim.md "cast, swim, burrow, breath and fly".
- [x] `tools/batch.py` `buildable_clips()` still models the old builders: teach it the new
      clips so `batch.py audit` stops listing them (owner of batch.py). Done (2026-11-04):
      left are attack_ranged 3, the flying sword's fly, the gibbering mouther's swim
      (amorphous has no swim builder) and the xorn.
- [x] (2026-11-04, clips3) Godot round 6 clip fixes (E): serpentine strike and swim, merfolk
      throw, legged flier idle = fly first frame, avian lunge rear, ghost idle from rest,
      half-held legs in falls. Gate on 14 pilots (not harpy, eagle in scratch): identical clip
      and frame counts; numbers change on constrictor-snake, merfolk (attack_ranged), ghost
      (idle, attack), centaur, wolf, dragon, owlbear and goblin (death). docs/rig-anim.md
      "Clip-to-clip jumps".
- [x] (2026-11-05, clips4) Godot round 7 clip fixes (E): legged idle starts on the rest
      pose (every layer 0 at t=0), floor one-shots start at the idle's body height
      (`at_idle_height`), the sword cut's arm IK fades in/out (`hand_ik`), the walk starts at
      the frame nearest the idle's first (`start_phase`), ground contacts (`GROUND`) bound how
      far the feet may lower the body. New report keys `from_idle_m`, `start_phase`,
      `from_idle_phase0_m`, `contact_min_z`, `ground_contacts`. Gate on 15 real pilots + the
      eagle in scratch: identical clip lists and frame counts; numbers change on the 7 legged
      pilots (harpy, goblin, wolf, owlbear, spider, dragon, centaur); legless/lifted only gain
      `from_idle_m`. docs/rig-anim.md "Clip-to-clip jumps, round 7".
- [x] (2026-11-05, clips5) Godot round 8 (E): new report key `from_idle_max_m` (clip frame 0
      against the whole idle cycle); the legged idle's tail sway is bounded
      (`tail_idle_amps`: root-to-tip ramp, far end <= 0.05 x body length). Gate on the 15
      pilots (the eagle for real): identical clip lists and frame counts; besides the new key
      only the dragon changes (idle `tail_raise_max_deg` 1.92 -> 1.72, `planted_foot_gap`
      0.0002 -> 0.0001, `from_idle_max_m` values). docs/rig-anim.md "round 8".
- [ ] Clips still without a builder: `fly` for plant_object (flying sword), `attack_ranged`
      without two arms (djinni, manticore, spirit naga), the xorn (radial); avian `cast`
      (giant owl) builds but no avian rig was available to test it.
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
      Godot round 5 confirms it: `grip_r` 2.9 cm from the skin in walk (11.7), 0.9/2.0/2.4 cm
      over the cut (3.3/26.9/6.7), and a longsword's hilt sits in the gauntlet.
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
        Godot round 5: heights match (eagle idle 0.357 / fly 0.373 m first frame), but an
        instant idle -> move switch still moves a vertex 1.07 m (eagle), 1.03 (merfolk) and
        0.34 (octopus) in one frame (round 4: 1.31 / 1.18 / 0.41); 0.25 s cross-fade 0.12-0.16 m
        as before. Pose, not height, now: leave open until the game decides on the blend.
  - [ ] Note: hunter-shark rest mesh is strongly curved (2.97 m tall for 4.5 m long). It
        swims level at 0.74 m and death lies belly-up on its dorsal fin.
- [x] Round 5 (2026-11-04, godot-dnd uncommitted, on top of 227a759): re-sync after df2a463,
      81c8194, 6a804d3; `check_monsters.gd` ALL PASS on 15 monsters and 32 props, every clip
      binds 100%. Verified in Godot 4.7.1 (check numbers + Xvfb screenshots):
      merfolk `hand_r`/`grip_r` and the registry spear in the fist (3.3-3.4 cm from the skin);
      harpy `grip_r` in the fist (longsword via the lab's new `loadout_override`); eagle
      stands on its talons at rest (lowest 0.000, 3.81 m tall) and idles at the fly height;
      ghost death flat (top 0.78 m, no sheet; hit min 0.000); fungus death with the root bone
      unturned (0.0 deg, 0.00 m); swarm death scale (1.7, 0.3, 1.7) + 12 deg; bow `string`
      node in all 4 LODs, hideable (`Monster.set_bowstring_visible`, lab `S`); whip,
      glaive, harpoon, greatclub forward (-Z) through the fist. godot-dnd: `load_prop` took
      exactly one mesh, so the new bows would not load (now stave + optional `string`);
      `prop_r=`/`prop_l=`/`bowstring=` in `lab_screenshot.gd`; the check attaches a longsword
      to unarmed rigs with `grip_r` and prints each death's end pose. New findings, open:
  - [x] Lifted idle vs grounded one-shots: eagle, octopus and merfolk attack_melee,
        attack_ranged, hit and death all start with the lowest vertex at 0.000 while idle
        hovers (0.357 / 0.348 / 0.180). Every settle back to idle lifts them and every
        one-shot drops them: largest vertex jump idle -> one-shot frame 0 is eagle 1.01-1.03 m,
        octopus 0.55, merfolk 0.19. Round 4 had them all on the floor. Lift the one-shots by
        the same `idle_lift` (death could still end on the floor), or ground the idle.
        (2026-11-04, anim4) Every clip of a lifted monster but the move clips is held at the
        idle's first-frame body height (`blender_clips.lifted_oneshot`, docs/rig-anim.md
        "Idle height"); death starts there and eases onto the ground. First / last frame
        lowest vertex: octopus attack, hit 0.000 / 0.000 -> 0.205 / 0.205, death 0.000 ->
        0.205 / 0.000 (idle 0.212); merfolk attack_melee, attack_ranged, hit 0.180 / 0.180,
        death 0.180 / 0.000 (idle 0.180); ghost death started 0.09 m above its idle, now at
        it (body 0.27 -> 0.18). Eagle: not re-run in out/ (another stream); a scratch run on
        a copy of its rig gives idle 0.357, attack and hit 0.362 / 0.362, death 0.362 /
        0.000, all at body 0.467, same frame counts. Re-check in Godot.
  - [x] Octopus idle vs swim still differ by 0.13 m at the lowest vertex (0.348 vs 0.215):
        the body lift matches, the tentacles hang lower in swim.
        (2026-11-04, anim4) Not the tentacles: the swim's bob starts at its bottom (-0.04 x
        height = -0.136 m). `match_move` now matches the move clip's first-frame body
        height (lift + its bob at t=0): idle body 0.341 -> 0.205, first-frame lowest vertex
        idle 0.212 / swim 0.215 (was 0.348 / 0.215). Both loops clean (delta 0). Matching
        the lowest vertex instead would lift the merfolk's idle 0.25 m (its swim pitches the
        body); merfolk, ghost, shark unchanged.
  - [x] Harpy left arm: `grip_l` 11.2 cm and `hand_l` 8.2 cm from the skin, nothing within
        10 cm of the grip (unchanged since round 4). The right-arm `rig_reweight` fix is
        needed on the left chain too before the harpy holds anything left-handed.
        (2026-11-04, anim4) Left chain `bone_8`/`bone_9` added to the override (`far` 0.5):
        `grip_l`/`hand_l` 11.5/8.2 -> 2.7/2.4 cm from the skin (Blender), bone_8/bone_9 own
        299/120 vertices (148/106, much of it on the wing). Edges past 4x: fly 223 -> 217
        (left 84 -> 78), sword cut 77 -> 85 (left 7 -> 15). With the default `far` 0.3 the
        fly went to 246 (left 107). Right side unchanged. Godot not re-synced.
        Godot round 7 (roll_1 + inserted wings, not this override): `grip_l` 1.8 cm and
        `hand_l` 2.2 cm from the skin, 16 vertices within 5 cm of the grip. A longsword on
        `grip_l` sits in the closed left fingers.
  - [x] Merfolk spear attack is a sword cut: overhead, then the point chops down to the floor
        in front (50%: tip (-0.35, -0.93, -0.11)) and sweeps across the body (75%). A spear
        wants a forward thrust (a per-category attack style). Idle holds the spear level above
        the head (grip at y 1.87 m), because the rest pose has the right arm raised.
        (2026-11-04, anim4) Attack style from the hand_r prop's category (polearm, thrown:
        thrust; else cut): the merfolk's attack_melee is a thrust, 30 frames as before. Grip
        drawn back to (0.28, 0.02, 1.51), driven to (0.10, 0.59, 1.58) at the thrust, moving
        within ~10 degrees of the shaft's +Y; spear tip 1.74 m forward vs the body's front at 0.67
        (ahead in every frame). Goblin cut and bow draw unchanged (frames and numbers). Arm
        relax: a raised rest arm is lowered (upper arm 30 degrees from the body, spear
        upright) in idle, swim, hit and the attack's ends; death fades it out. Idle grip
        z 1.87 -> 1.48 m (lift included). Still open: attack_ranged (the throw) starts from
        the raised rest arm, so idle -> attack_ranged raises the arm in one frame; the left
        arm, held out sideways at rest, is left as it is.
  - [ ] Bow string: straight, 0.18 m behind the grip (longbow, 1.8 m wielder), so in the goblin's
        attack_ranged it passes through the left forearm and shoulder. Fine for a static
        string; a drawn string needs the game to replace it.
  - [ ] Whip is one rigid mesh: the lash sags forward at idle but in attack_melee it is a
        straight 1.4 m rod overhead (goblin scale). Needs lash bones or game-side simulation.
  - [x] Swarm death squashes each rat to 30% height (one bone, uniform scale): reads as a
        pile of flattened rats, not scattered ones. Expected with the one-unit swarm mesh (H).
        (2026-11-05, Godot round 9) Verified on the first swarm made as a unit: swarm-of-bats
        (one 0.3 m bat, 47 bones, manifest `swarm_unit`) synced, and godot-dnd's
        `Monster.spawn_swarm()` shows 12 bats over the 1.5 m footprint with staggered phases;
        the bat's death ends on its back at full size (0.23 x 0.17 x 0.19 m), the flyers fall
        to the floor. swarm-of-rats is still the column pilot (squash and all) until it is
        regenerated with a unit (H).
  - [ ] Fungus death: the cap ends standing on its rim as a vertical disc 1.22 m high next
        to the planted base. Plausible for a fallen mushroom; noting it in case a flat cap is
        wanted.
- [x] Round 6 (2026-11-04, godot-dnd uncommitted, on top of 40980df): re-sync after ede55a9,
      71dd174, 1def4a4 (`--ids`: snake, centaur, eagle, merfolk, octopus, swarm, ghost; harpy
      not synced, rig being rewritten). `check_monsters.gd` ALL PASS on 16 monsters and 32
      props, every clip binds 100% (centaur 21/19/19/21/17, snake 6/15/15/15/15/15, eagle
      9/8/17/10/17). Verified in Godot 4.7.1 (check + Xvfb screenshots): snake slither (S-wave
      in the top view, the whole 4 m on the floor, lowest 0.000 in every clip); centaur walk
      with four hooves cycling and the thrust on the right arm (manifest `style: thrust` for
      the pike, not a cut), the lab refuses a left-hand prop with a warning; eagle stands on its
      talons, idle and fly hover ~0.35 m, the lunge leads with the beak at 50%, wings
      symmetric; merfolk idle holds the spear upright (tip (0.07, 0.95, -0.31), grip y 1.52 m),
      thrust tip forward (-0.18, 0.23, -0.95); lifted one-shots: idle -> attack/hit frame-0 jump
      octopus 0.55 -> 0.225 m (no height pop in the side shots), merfolk 0.19 -> 0.059, eagle
      one-shots start at 0.348 (idle 0.352; jump 0.938 m is pose). No godot-dnd code change.
      New findings, open:
  - [ ] Centaur tail has no bones (`anim.parts.tail` empty): the tail sheet follows the near
        hind leg in walk, swept forward under the belly at 0% and kicking forward like a hoof
        at 50% (confirmed). (2026-11-04, clips3) Nothing for the clips to drive: SkinTokens
        found no tail chain and the tail is skinned to the hind leg. A rig fix (H, centauroid).
  - [ ] Centaur arrow-shaft stick shreds into a fan of dark spikes in front of the chest in
        attack_melee 25-75% (clean reference needed, as noted in H).
  - [ ] Centaur `grip_r` is 13 cm from `hand_r`, 5.4 cm from the nearest skin at idle (0
        vertices within 5 cm; goblin 2 cm), 8-9 cm during the thrust.
  - [x] Centaur death dips to -0.033 m mid-fall (check WARN); idle/walk sit at -0.004/-0.006.
        (2026-11-04, clips3) A half-held leg (IK fading 0.28-0.45) rolled with the body and
        its hoof dipped at 0.33, which raising the body could not lift. In a `rest` fall a leg
        whose own vertices dip while its IK is between 0 and 1 now has its foot target raised
        by the dip each settle pass: lowest -0.033 -> -0.010 m (the buckle before the roll, as
        before), foot raise up to 0.134 m. Same rule: wolf death -0.016 -> -0.006, dragon
        -0.010 -> -0.006, owlbear and goblin feet up 0.024 / 0.014 m, lowest unchanged.
  - [x] Centaur loadout: the registry pike is a two-hander on a one-armed rig, and godot-dnd's
        `scale_m / 1.8` prop scale gives 1.333 (scale_m is the whole 2.4 m centaur), a 4.7 m
        pike. Needs a wielder (rider) height for centauroids, upstream or in the game.
        (2026-11-05, Godot round 9) Props scale by `rider_height_m / 1.8` when set (override
        field; godot-dnd's sync copies it into monster.json), else `scale_m / 1.8`, in
        godot-dnd (`Monster.prop_scale()`) and the viewer (index field `rider_height_m`,
        `updateProps`). Centaur pike 4.67 -> 3.50 m (scale 1.333 -> 1.000) in both, checked
        in screenshots. Still one-handed (no left arm, no off-hand IK).
  - [x] Eagle idle -> fly instant switch moves a vertex 1.754 m in one frame (round 5 1.070,
        the eagle is 1.39x bigger); 0.25 s cross-fade 0.182 m. Pose (wings), not height.
        (2026-11-04, clips3) The idle's slow beat swells back to full size at its loop start,
        so its first frame is the fly's (`fly(amp < 1)`), and the lifted one-shots hold the
        fly's first-frame wings (`wing_beat(at=0)`). Scratch copy of the eagle (out/ not
        re-run: another stream re-scales it), first-frame jump from the idle (Blender): fly
        0.616 -> 0.009 m, attack/hit/death 0.938 -> 0.137. Re-check in Godot after the
        eagle's re-run.
        Godot round 8 (re-scaled rig): idle -> fly frame 0 0.010 m, one-shots 0.072 m. Idle 0%
        and fly 0% screenshots are identical. The probe (switching after 0.5 s of idle) still
        gives 1.121 m instant and 0.192 m cross-faded, because the idle itself beats its
        wings (1.69 m from its own first frame mid-cycle).
  - [x] Eagle attack_melee at 25% draws the head back behind the near wing with a dark gap
        (tear) at the neck.
        (2026-11-04, clips3) The avian lunge rears neck and skull `AVIAN_REAR` 0.35 as far
        (30 -> 10.5 degrees): edges stretched past 4x at 25% 219 -> 3, past 2x 1033 -> 67
        (scratch). The strike frames still stretch the raised wings (88 edges past 4x, was 153).
        (2026-11-05, clips4) On the re-scaled rig (21:59, scratch copy) the lunge has 17 edges
        past 4x (vs bind pose) in every frame, the strike included: that is the held fly
        first-frame wing pose (fly 4-17), the lunge adds none; head forward travel 0.26 x body
        length. No change; Godot to re-check on the new rig.
        Godot round 8: no neck gap at 25%, beak forward and down at 50%, wings hold. It reads
        as a small peck.
  - [x] Eagle scale: 5.84 m wingspan (right for 20 ft), 5.29 m tall to the raised wing tips,
        head ~3.5 m; next to the 1 m goblin its legs alone are the goblin's height. Check
        that this is the intended size.
        Godot round 8: avian max-dimension scale, bbox 3.00 x 2.97 x 1.54 m (56 bones). The
        head is about at the 1.8 m capsule's top, and the goblin reaches the eagle's belly
        (eagle-and-goblin screenshots).
  - [x] Snake strike is small: only the front 5 bones key (6 tracks), the head rears 0.3-0.5 m.
        Snake idle -> swim frame-0 jump 1.245 m (walk 0.717).
        (2026-11-04, clips3) Strike on the front third (a cobra S), a sideways coil and a
        draw-back as the wind-up, forward and down with a 0.08 x length slide: head tip
        wind-up -> bite 0.81 -> 1.39 m (0.35 x length), rise 0.30 -> 0.63 m, rear on the
        ground (`contact`). The swim had no lift (it is `contact`); the jump was the wave:
        18 degrees per joint over one wave swung the tail 2 m. Swim is now 16 degrees over 1.5
        waves growing toward the tail, and walk and swim start at the phase nearest the rest
        pose; the idle is a standing flex starting on the rest pose. Idle -> swim 1.245 ->
        0.320 m, -> walk 0.717 -> 0.379, -> attack/hit/death 0.270 -> 0. docs/rig-anim.md
        "Clip-to-clip jumps".
  - [x] Ghost death now starts at lowest 0.180 m vs idle 0.254 (jump 0.104 m, same as hit).
        (2026-11-04, clips3) The body heights matched; the idle's hem sway lifted the train on
        its first frame. Every idle layer now starts at 0 (`from_rest`): idle first lowest
        vertex 0.254 -> 0.180, idle -> hit, death 0.104 -> 0, -> attack 0.044 -> 0 (its hem
        sway too); idle -> fly 0.702 -> 0.801 (the fly's lean).
  - [x] Merfolk attack_ranged still starts from the raised rest arm (0.614 m jump; open in
        round 5 notes).
        (2026-11-04, clips3) `attack_throw`: an FK overhand throw from and back to the relaxed
        arm (cock behind the shoulder, release at head height, follow through), 30 frames:
        idle -> attack_ranged 0.614 -> 0.059 m (= attack_melee, hit).
  - [ ] Snake, centaur and eagle are described as unpainted, but render with full texture
        colours (python pattern, palomino and rider, bald eagle) in Godot: the texture stage
        alone is enough for a read. Not a bug.
- [x] Round 7 (2026-11-04, godot-dnd uncommitted, on top of 561f85f): the harpy only, after
      9213dc2 and 1638b68 (roll_1, wing chains from the mesh, seam blend). `sync --ids harpy`
      updated 11 files. `check_monsters.gd harpy`: ALL PASS. 32 bones (was 38), including
      `wing_l_00..03`/`wing_r_00..03` on `bone_3`. Every clip binds 100%: attack_melee 14/14,
      death 17/17, fly 16/16, hit 17/17, idle 17/17, walk 10/10. Fly keys all 8 `wing_*`
      bones. LOD0-3 share the skeleton and skin, 32 binds each with the wing bones (LOD1-3
      p95 0.002-0.008 m). One WARN, as in round 5: fly starts 0.36 m above idle.
      - Sockets, nearest skin at walk: `hand_r`/`hand_l`/`grip_r`/`grip_l` 3.1/2.2/2.2/1.8
        cm (round 5: 1.9/8.2/2.9/11.2). `grip_r` over the cut at 25/50/75%: 2.2/2.2/1.8 cm.
      - Transition idle -> walk per frame: 0.206 m instant, 0.043 m with the 0.25 s
        cross-fade (round 5: 0.139/0.044).
      - Verified with Xvfb screenshots:
        - Fly beats the wings without the arms. Both hands hold x 0.48/-0.37 through the
          clip and move only with the body's bob.
        - The sword arm rises clear of the wing, and the right wing stays put.
        - The longsword hilt is in the right fist at idle and over the cut, and in the left
          fingers on `grip_l`.
      New findings, open:
  - [x] Harpy fly still tears at the hanging wing tips next to the legs (Blender 96 edges).
        (2026-11-04) Not wing feathers holding leg weight: the torn strip was the hip feathers
        (0.5-0.9 m from a leg over the surface, 1.0-1.4 m from the wing) that the inserted
        chain's euclidean region gave pure `wing_*_03` weight next to thigh/hip-weighted
        neighbours; plus a floor feather of the wing on the ankle (`bone_20` 1.0). Body seam
        (C): fly 96 -> 4 edges past 4x, walk 5 -> 0; the 0% blades and the 25% strip are gone
        in Blender sheets. Godot to re-check.
        Godot round 8: no blades at 0% and no strip at 25% (front). Only the wings' own long
        tips hang, one to knee height at 25%. Walk side 0/50%: the feathers hang to the
        floor with tapered tips and no strip.
        - At 0% (wings spread), the lower feathers stretch sideways from the hips into flat
          blades out past the wing span, longer on the right.
        - At 25% a thin strip runs from the hip down to the floor below the feet (side view).
        - At 50% a dark sliver crosses in front of the shins at ankle height.
        - Walk 50% shows the same kind of strip, from the knee to the floor (side view).
  - [x] Harpy sword cut: the raised right armpit pulls a flat grey sheet. At 25% it hangs
        down the side of the torso, and at 50% it spans across the chest under the arm. This
        is likely the right side of Blender's 48 edges. The new clip raises the fist above
        the head (grip y 1.73/1.62 m at 25/50%, round 5: 1.27/1.18), and at 50% the fist is
        in front of the face. Round 5's lower cut showed no sheet.
        (2026-11-04) SkinTokens' weights: the back on the centre line (x 0.0-0.1, z 0.8-1.1)
        held 0.5-0.82 of the right upper arm `bone_11`, and the waist (0.28, 0.11, 0.86) 0.33 of
        the forearm `bone_12` hanging beside it; the raised arm pulled them up (worst at frames
        4-10). The body seam's arm -> body (C) frees them: cut 48 -> 14, the sheet is gone at
        25% and 50% in Blender close-ups. The 14 left are on top of the right shoulder
        (`bone_11` vs `wing_r_00`/`bone_3`, the wing root beside the shoulder); a root 6 cm
        farther back gave 11 and looked the same (not kept). Godot to re-check.
        Godot round 8: no armpit sheet in the longsword cut at 25% or 50% (three-quarter).
  - [x] Harpy idle frame 0 -> attack_melee frame 0 now jumps 0.167 m (round 5 0.014), and
        -> walk 0.175 m (0.113). Walk dips to -0.023 m (round 5 -0.008), still inside the
        check's -0.03 m limit.
        (2026-11-04) Attack: the 0.167 m vertex was the back skin with 0.82 upper-arm weight
        (skinning, fixed by the body seam); now 0.137 m at the right hand (`hand_r` 1.0): the
        clip's frame 0 has the hand 5 cm and `grip_r` 7.8 cm from idle's frame 0 (clip side,
        for the clip stream). Walk: the left foot (`bone_16`/`17` heads 0.17 m from idle), the
        cycle starting mid-stride (clip side). Walk's lowest point is a hip feather tip lying on
        the floor at rest, skinned to the pelvis `bone_0` (rest (0.48, -0.17, 0.0)), -0.027 m at
        frame 14 in Blender (unchanged by the body seam): the pelvis bob takes it under the
        floor; the clip grounds the feet only.
        (2026-11-05, clips4) Blender, idle frame 0 -> clip frame 0: attack 0.137 -> 0 (the cut's
        arm IK chose its own elbow at full influence; it now fades in and out), hit 0.060 -> 0
        and death -> 0 (one-shots start at the idle's body height), walk 0.175 -> 0.122 (starts
        at the cycle frame nearest the idle; with two legs one foot is always off its rest).
        Walk min_z -0.0297 -> -0.010: the two floor feather tips are ground contacts, the body
        may not go lower for the feet than keeps them at -0.01, so the standing feet are up to
        2.2 cm short at the stride ends (`planted_foot_gap` 0.022). Stride 0.8, 33 frames as
        before. Godot to re-check.
        Godot round 8: idle -> attack, hit, death frame 0 = 0, walk 0.122 (the same as
        `from_idle_m`), walk lowest -0.010 m. In the side view the near wing hides most of
        the feet, so the 2.2 cm gap cannot be seen.
  - [x] Harpy arms still not mirrored: `hand_r` at x +0.48 and `hand_l` at -0.37, elbows
        `bone_12`/`bone_8` at +0.37/-0.25 (round 5 hands +0.51/-0.48). The left fist sits
        nearer the body.
        (2026-11-04) Mesh, not skeleton: the body's centre line is at x +0.053 (spine bones
        +0.068), because the origin is the bbox centre and the wings are not mirrored (right
        wing 0.86 m from the centre line, left 0.97). About the centre line the arms mirror:
        hands 0.428/0.427 m, elbows 0.32/0.31, shoulders 0.19/0.16, legs within 3 cm. No `fit`.
- [x] Round 8 (2026-11-05, godot-dnd uncommitted, on top of ff09909): re-sync after
      1def4a4..f8bb67e. Synced with `--ids` for the 15 pilots; centaur and the GPU stream's
      monsters were not synced. The smoke-run monsters have no rig.
      - Updated: eagle (rig, LODs, clips), harpy (rig, LODs, 5 clips; fly unchanged),
        snake, merfolk attack_ranged, goblin, wolf, owlbear, spider, dragon (clips and
        LOD2-3), ghost (idle, attack, death).
      - `check_monsters.gd` ALL PASS on 15 monsters and 32 props. Every clip binds 100%.
        3 WARNs: harpy/dragon fly and merfolk swim heights, as before.
      - godot-dnd change: the check's "clip moves a bone" probe falls back to 30% when 50% is
        the rest pose. The snake's standing-wave idle failed it.
      - **Godot frame-0 jumps equal `clips.json` `from_idle_m` on every clip of every monster.**
        - Legged one-shots: 0, except dragon death 0.042. Round 6/7 values: goblin cut
          0.522, dragon 0.76-0.79, harpy 0.167/0.060, wolf/owlbear/spider 0.05-0.07.
        - Idle -> walk: goblin 0.099 -> 0.072, wolf 0.246 -> 0.197, owlbear 0.267 -> 0.219,
          spider 0.414 -> 0.093, harpy 0.175 -> 0.122, dragon 0.423 -> 0.435.
        - Snake: one-shots 0.270 -> 0, walk 0.717 -> 0.379, swim 1.245 -> 0.320.
        - Merfolk throw: 0.614 -> 0.059.
        - Ghost: one-shots 0.104 -> 0; fly 0.702 -> 0.801.
      - Verified with Xvfb screenshots:
        - Goblin idle/attack_melee/attack_ranged/hit/death frame 0 are pixel-identical.
        - The snake's cobra strike rears about 0.6 m and strikes forward and down.
        - The merfolk throw goes cocked -> release -> follow-through, spear in the fist.
        - Harpy and eagle as noted in the items above.
      New findings, open:
  - [x] Dragon idle tail: the in-phase 5-degree sway adds up along the 9-bone curled tail.
        The tip (`bone_66`, lying on the floor at the left side) swings 1.66 m sideways at
        0.62 s and 1.34 m the other way at 1.88 s.
        - Frame 0 matches (one-shots 0), but a game switch is mid-idle. Godot's probe (walk
          started after 0.5 s of idle) went 0.955 -> 1.506 m instant and 0.102 -> 0.269 m
          with the 0.25 s cross-fade. Attack/hit/breath from mid-idle jump up to 1.66 m.
        - The other legged idles wander 0.02-0.10 m. A smaller tail amplitude per joint
          (scaled by chain length) would fix it.
        (2026-11-05, clips5) `tail_idle_amps`: still in phase (frame 0 = rest), but growing
        root -> tip (i / n, the last bone at most 5 degrees) and scaled so the chain's far end
        moves at most 0.05 x body length (0.29 m on the dragon). Tip travel 1.625 -> 0.243 m;
        idle `from_idle_max_m` 1.657 -> 0.305 (now the head's look-about); one-shots from
        mid-idle 1.66 -> 0.305-0.315, walk 1.698 -> 0.618. Frame-0 values unchanged. Godot
        to re-run its mid-idle probe.
  - [x] Note (not a bug): `from_idle_m` is frame 0 against frame 0. The game fires
        one-shots mid-idle, so what it sees is idle(t) -> clip(0).
        - Worst over the idle cycle (Godot scratch scan): goblin 0.05, wolf 0.09, owlbear
          0.07, spider 0.02, harpy 0.10, ghost 0.23, snake 0.28, octopus 0.36, shark 0.28,
          eagle 1.68 (the idle beats), dragon 1.66 (tail).
        - Reporting a per-clip `from_idle_max_m` over the idle cycle would expose cases like
          the dragon's.
        (2026-11-05, clips5) Done: every clip report and the manifest have
        `from_idle_max_m` (clip frame 0 against every 3rd idle frame; on the idle, its own
        wander). Before the tail fix it matched the Godot scan (dragon 1.657, eagle 1.683,
        snake 0.275, ghost 0.226). Per-pilot table: docs/rig-anim.md "round 8".
  - [x] giant-eagle `anims/clips.json` (22:03) predates `from_idle_m`, so it has no Blender
        jump values. Godot measures 0.072 (one-shots) and 0.010 (fly), matching the
        scratch-run numbers in docs/rig-anim.md.
        (2026-11-05, clips5) Re-run for real: fly 0.0096, attack/hit/death 0.0716 m
        (`from_idle_max_m` fly 1.701, one-shots 1.683: the idle's own beat), 6 ground
        contacts, same clips and frame counts.
  - [ ] The eagle lunge is a small peck: head travel 0.26 x body length, and from the side
        the strike barely leaves the raised-wing silhouette. Fine for a bite; a giant eagle's
        beak/talon attack may want more reach.
- [x] Round 9 (2026-11-05, godot-dnd uncommitted, on top of fcce8c4): re-sync after
      f8bb67e..cade6ef with `--ids` for the 16 synced monsters plus swarm-of-bats (new;
      centaur 5 clips, dragon 1 clip, spear prop updated). `check_monsters.gd` ALL PASS on 17
      monsters and 32 props, every clip binds 100%, the same 3 fly/swim WARNs.
      - Swarm units: sync copies manifest `swarm_unit` into monster.json; `Monster.spawn_swarm(12)`
        lays units on a seeded sunflower spiral over the `swarm.scale_m` disc (flyers and
        swimmers also over a height band), slaved to the leader's clip with golden-ratio
        phases on loops and 0-0.35 s delays on one-shots. The check verifies the footprint
        (bats 1.21 x 0.88 x 1.46 m in 1.5) and 12 distinct poses.
      - `rider_height_m`: see the centaur loadout item above.
      - Dragon mid-idle (idle-cycle scan): one-shots worst 0.307-0.317 m (round 8 1.66), walk
        0.618 (about 1.7), idle wander 0.307; probe idle -> walk 0.578 instant / 0.084
        cross-faded (round 8 1.506 / 0.269, round 6 0.955 / 0.102). Fixed.

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

### G. Non-SRD sources (moved)
Moved to the private non-SRD checkout (`../monster-forge-nonsrd` or `$MONSTER_FORGE_EXTRA`):
its `docs/TODO.md` has this section verbatim, including the SRD cross-check.

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
      some bunching in the slither. Needs a pose ControlNet / better model for snakes. (2026-11-04) Done: the `serpentine_side` depth
      template gives side-view snakes stretched along the ground (3 of 4 in round 2); the
      mesh is a 4.0 m long body (0.40 x 0.56 m section), spine_chain 16 bones, no fused coils,
      slither verified in sheets. Paint pending (see B).
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
      skips wing chains (head `bone_11` -> `bone_5`, the beak). Its mesh was 29.5 degrees off
      +Y: (2026-11-04) yaw pinned -150.5, facing 0.0, wings symmetric, one leg per side, rig
      roll_0 score -1.70 (was -3.32). It is now 1.39x larger (scale_m is nose-to-tail: 5.84 m
      wingspan, 5.78 m tall); check that against the game's expectations. Paint pending (B).
      (2026-11-05) Fixed: avian is in `forge.MAX_SCALE_PLANS` (largest dimension = the spread
      wingspan; `custom.theropod` keeps nose-to-tail, `forge.scale_axis`): wingspan 3.00 m,
      height 2.97; rig re-rolled (roll_1, -1.38, both wings, spine only), anim re-made.
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
- [ ] centauroid (centaur-trooper): (2026-11-04) **a real centaur mesh** from the `centaur_34`
      depth template (round 2 ref_2: human torso where the neck would be, 4 hooves down, empty
      hands, photo-like rather than the ink style), yaw 100.5; SkinTokens roll_1: 4 legs,
      spine, head, right arm only (an arrow shaft behind the head meshed as a sideways stick
      where the left arm should be), so `attack_ranged` is missing; the tail hangs to the
      ground and moves with the hind legs. Open: a cleaner reference (no shaft, house style)
      and a re-roll for both arms. Paint pending (B). The tail has no bones (no tail chain in
      roll_1; it is skinned to the near hind leg, so it kicks with the hoof in the walk, Godot
      round 6): the clips cannot fix it, the re-roll or a fallback tail chain must. Before: juggernaut drew a man riding a horse in
      8 of 8 over three prompts. A rider proxy (prancing, one hoof up) rigged with both arms,
      head and spine; the stage refuses its 3 grounded feet. In scratch with feet 3 all six clips
      build (sword cut and bow draw on the arms). Batch: blocked on references (Z-Image /
      FLUX.2-klein or a pose ControlNet); a four-hooves-down stance prompt.
      (2026-11-05) No-quiver re-reference (docs/references.md rounds 4-5): no weapons in 4 of
      4, clean mesh, but the arms hang against the body and 8 of 8 rolls found no arm (5
      "legs"): not kept, the round-2 pilot stays. Still open: a reference with the arms clear
      of the body (a centaur depth template with the arms out further, or an OpenPose arm
      hint on top of the depth one) and a tail chain (12 rolls, none: rig-stage insertion,
      like the wing chains, for another stream). Prop scale: props scale by `scale_m / 1.8`
      and the centaur's scale_m is its whole 2.4 m body, so its pike came out 4.7 m. New
      override field `rider_height_m` (centauroid, merfolk_naga; centaur-trooper 1.8): the
      viewer and the game should scale props by `rider_height_m / 1.8` when set
      (docs/equipment.md). The viewer change is not done (viewer/ is another stream's).
- [x] merfolk_naga (merfolk-skirmisher): SkinTokens (spine, tail, arms); legless builders with the
      biped sword cut, tail kick swim. Both arms found since the raised-arm fallback (C), all
      sockets placed. Batch: SkinTokens; the marilith's 6 arms untested.
- [x] swarm (swarm-of-rats): `fallback:root` + root-only clips. The reference draws a wall of
      rats (one cluster mesh); the batch should model one unit with the unit's plan and leave
      the swarm to particles. Death scatters and sinks (1.7x wide, 0.3 high) rather than
      squashing the pilot's column flat. (2026-11-05) Swarm units done for the other 6
      (`forge.SWARM_UNITS`, docs/references.md "Swarm units"): the record is made as its unit
      with the unit's plan and scale; swarm-of-bats went through rig and anim on the avian
      path (both wings, 5 clips). **swarm-of-rats should be regenerated as a unit** (`{"unit":
      "rat"}`: quadruped, 0.25 m) with `run --stages ref,mesh,qa,lod,rig,anim --force`; it is
      left out of the table until then so the pilot's column keeps its swarm rig.
- [x] Fix found on the way: `blender_rig.add_sockets` renamed the head to `head` and then
      failed to parent `back` to it (KeyError), now aliased. The bone was the head after all
      (it owns the skull); `back` went there because the left "arm" was a hair chain off the
      head. Fixed in `find_arms` (C): `back` is on the chest now.
- [x] Stale `manifest.errors` entries on ghost, merfolk and snake removed (their rig stages
      succeed); rig and anim now clear their own entries on success (C).

### I. Batch (2026-11-04)
`tools/batch.py` runs the SRD batch (`plan`, `run`, `report`, `audit`); coverage audit, tiers and
the run book in `docs/batch.md`. 315 monsters, ~35.1 h for tiers 1-3 (301), ~36.3 h with the hold
tier. (2026-11-05) `batch.py` follows cade6ef: tiers 1 = 155, 2 = 113, 3 = 33, 4 = 14 (was 146 /
101 / 31 / 37); `hold_reason()` keeps only centauroid (3), serpentine (10) and the snake swarm
(1), and `classify()` / the estimates take a swarm as its unit (`forge.resolve_swarm_unit`).
Blocking gaps (reference-stage fixes; their records are tier 4 and not run by default):
- [x] Avian reference prompt (16: every avian but the eagle and the 2 theropods): (2026-11-05)
      per-plan tweak `prompt_tweaks.json` `_plans.avian` (front view, both wings spread to
      the sides, feet on the ground): giant-vulture, raven and the bat unit 4 of 4 on pose;
      giant-vulture meshed at 2.40 m wingspan, facing 0.5 deg. **Unblocked** in `batch.py`: 7
      to tier 1 (as giant-eagle), roc / hawk / owl / raven tier 2 (size), the 4 bat-winged tier 2
      (same path), wyvern tier 3 (untested template).
- [ ] Serpentine (10): release once constrictor-snake is through anim with the `serpentine_side`
      depth template.
- [x] Swarms (6): (2026-11-05) one unit per swarm (`forge.SWARM_UNITS`, override `swarm_unit`):
      bats (avian, through anim) and insects (a beetle, through qa; yaw pinned) checked.
      **Unblocked**, with the unit plan's own tier: bats/ravens avian (now prompt-fixed),
      piranhas aquatic (shark path), venomous snakes serpentine (still held with the
      serpentine plan), crawling claws `custom.limb_chain` (untested, as the troll limb).
      In `batch.py`: bats, ravens, insects, piranhas tier 2 ("same path" as the unit plan's
      pilot; the units are tiny and a swarm is tier 2 at best), crawling claws tier 3
      (`UNTESTED_IDS`), venomous snakes tier 4 (serpentine unit).
- [ ] Centauroid (3: centaur-trooper, drider, lamia): the centaur-trooper pilot through anim
      on its new centaur reference. (2026-11-05) Still held: the pilot is through anim with
      one arm and no tail chain; the no-quiver reference lost both arms (H). Lamia (lion body,
      no weapons) could run as tier 3 if one arm is acceptable.
- [x] Specter, wraith (2): (2026-11-05) per-id copies of the ghost's legless-robe tweak; specter
      round 3 and wraith round 1-2 give floor-length robes without feet as the ghost pilot,
      `ref_pick` 3 each (not meshed). **Unblocked** (custom.ghost path, tier 1 in `batch.py`).
Fixable after the batch (re-run `anim` or `rig,anim` only; counts and lists: `batch.py audit`):
`attack_ranged` 3 clips; winged_biped wing chains (20: the rig stage now
inserts them from the mesh, C; check the arm-wing seam in each sword cut); tiny / huge / gargantuan budgets; the 33
untested variants; off-model fork, heavy crossbow, mace props.
- [x] (2026-11-04, clips2) `cast` 48, `swim` 39, `burrow` 18, `breath` 11 and `fly` 8 of 9 (not
      the flying sword) have builders (D); the anim thresholds are size-relative outside the
      pilots' band (tiny / huge / gargantuan). Records made before this need `anim --force`;
      `batch.py`'s `buildable_clips()` learnt them (audit and docs/batch.md refreshed).
- [x] (2026-11-04) Smoke run: 6 tier-1 records through `ref,mesh,qa` (docs/batch.md "What to
      watch"). batch.py fixes: ComfyUI restart before mesh (a mesh after a ref stalled in
      MemoryHigh reclaim), `/free` at once in the memory gate, the facing gate right after qa,
      stale mesh / qa detection (re-rolled ref, new `ref_pick`, pinned `yaw`) run with
      `--force`, a stop after 3 monsters in a row fail within 10 s, report fixes.
- [x] Ghoul reference: every candidate is a bare skeleton, also with `prompt_extra` /
      `negative_extra`. (2026-11-05) Full prompt in `data/prompt_tweaks.json` (round 2: no
      "undead", a fleshy grey face, skull / statue negatives): 4 of 4 fleshy clawed humanoids,
      empty hands; ref_2 meshed at 1.8 m, facing 0.5 deg (the old smoke-run mesh replaced).
- [ ] References drift off-model on "proven" plans (3 of 6 in the smoke run: skeleton ghoul,
      top-down phase spider, lizard gibbering mouther). Review every candidate strip and
      thumbnail before the paint pass; consider a reference check (e.g. a SigLIP prompt
      match per candidate like the unarmed check) before tier 1 at scale.
- [ ] Facing check sign: with `head_known` false the suggested yaw can be 180 degrees off and
      the gate passes either way (phase-spider); owner of rig.py.
- [ ] First batch run: `batch.py run --tier 1 --stages ref,mesh,qa` (~4.4 h), review the strips,
      then the paint pass once the ComfyUI memory limit is decided; `report` after the first 20.
