# Resonant: Vertical Slice Script (V1)

> **Total Dialog Payload:** ~3,100 bytes (still <1.2% of 256KB resource limit)  
> **Design Goal:** Slow-burn character arc, layered world-building, clear mechanical progression, and OPR as a recurring threat.

---

## I. Global Map Flags

```c
bool flag_mage_joined = false;      // Mage following
bool flag_ward_open = false;        // Door unsealed
bool flag_statue_examined = false;  // Lore flag
bool flag_midboss_defeated = false;
bool flag_guard_awakened = false;
bool flag_terminal_1 = false;
bool flag_terminal_2 = false;
bool flag_boss_defeated = false;
bool flag_opr_escaped = false;      // Set after boss
bool flag_gear_check = false;       // For tutorial tease
```

---

## II. ACT I: Outer Ruins (Atlas: `ATLAS_OUTER_RUINS`)

### Event 01: The Hook (Combat Tutorial)
- **Trigger:** Spatial bounding box at entrance
- **Condition:** `!flag_mage_joined`
- **Script:**
    - **VAN:** "Always a complication." (22 bytes)
- **Post-Script:** Drone encounter (basic combat tutorial). Reward: 5 scrap.

---

### Event 02: The Dampening Ward (Meet the Mage)
- **Trigger:** Automated post-combat (after first fight)
- **Condition:** `!flag_mage_joined`
- **Script:**
    - **MAGE:** "I had that." (11)
    - **VAN:** "You were cornered by baseline drones. You didn't have that. Who are you?" (74)
    - **MAGE:** "An independent researcher. This door is sealed via biometric Resonance. Which means you, mercenary, are locked out." (120)
    - **VAN:** "I have explosives." (18)
    - **MAGE:** "Kinetic-dampening ward. Blow it up, it seals permanently. I can unbind it, but the casting takes time – time I don't have if more drones show up." (154)
    - **VAN:** "I keep the scrap off you, you open the doors. We split whatever's in the vault." (84)
    - **MAGE:** "You drive a hard bargain. Fine. But don't get in my way." (56)
- **Post-Script:** `flag_ward_open = true`, `flag_mage_joined = true`, door tile updated.

---

### Event 03: Ruined Statue (Lore – Slow Build)
- **Trigger:** Player action facing target cell
- **Condition A:** `!flag_mage_joined`
    - **SYSTEM:** "Scorched from the inside out. The stone is still warm." (54)
- **Condition B:** `flag_mage_joined && !flag_statue_examined`
    - **MAGE:** "Class-4 Arcanist. Tried to breach the Inner Sanctum alone." (57)
    - **VAN:** "And got cooked from the inside. What's your point?" (47)
    - **MAGE:** "Resonance isn't a tool. It's a pact. Break it, and you become that." (69)
    - **VAN:** "You're full of cheery warnings. Let's move." (43)
- **Post-Script:** `flag_statue_examined = true`

---

### Event 04: The Courtyard (Swarm Encounter)
- **Trigger:** Spatial chokepoint
- **Condition:** `flag_mage_joined`
- **Script:**
    - **MAGE:** "Too many. If they swarm us, your blade won't be fast enough." (61)
    - **VAN:** "Then do that independent research you're so proud of and thin them out." (72)
    - **MAGE:** "Don't patronize me. Just keep them off my back." (47)
- **Post-Script:** Swarm encounter. Unlock Chain Lightning mechanic (Mage AoE). Reward: 10 scrap.

---

### Event 04B: The Vault Guardian (Midboss – First Big Fight)
- **Trigger:** Spatial box at Inner Sanctum entrance
- **Condition:** `flag_mage_joined && !flag_midboss_defeated`
- **Script:**
    - *(Engine: screen shake, ground cracks)*
    - **MAGE:** "The seal is old. Someone's been feeding it power – it's awake." (62)
    - **VAN:** "So the defense grid has a pet. Great." (36)
    - **GRD:** "INTRUSION DETECTED. DEPLOYING ANCHOR UNIT." (46)
    - **MAGE:** "Class-3 combat chassis. If it grapples you, you're done." (55)
    - **VAN:** "Then I'll make sure it's holding a grenade when I pull the pin." (58)
    - **MAGE:** "You're insane. I'm trying to keep us alive!" (43)
    - **VAN:** "Then keep casting and stay out of my way." (40)
- **Post-Script:** Midboss encounter (teaches party synergy: Van tanks, Mage supports, use of items). `flag_midboss_defeated = true`. Unlock Resonance explanation (brief tutorial: Van's latent energy).

---

### Event 04C: Aftermath – Cracks in Trust
- **Trigger:** Post-midboss return to field
- **Condition:** `flag_midboss_defeated && !flag_boss_dead`
- **Script:**
    - **MAGE:** "You fought like you've done this before. A lot." (48)
    - **VAN:** "I've had practice. You're not bad for a researcher." (47)
    - **MAGE:** "Flattery won't get you a discount on my share." (41)
    - **VAN:** "I'm not paying you. You're paying me by opening doors." (50)
    - **MAGE:** "We'll see who's paying whom when we're inside." (44)
- *(No flag change – just character moment.)*

---

## III. ACT II: Inner Sanctum (Atlas: `ATLAS_INNER_SANCTUM`)

### Event 05: The Breach (Environment Shift)
- **Trigger:** On scene load
- **Condition:** `!flag_boss_dead`
- **Script:**
    - **MAGE:** "The Inner Sanctum. Architecture intact – means the defense grid is fully online." (73)
    - **VAN:** "No more rusted scrap. Check your gear – and your attitude." (58)
    - **MAGE:** "My attitude? You're the one who charged a Guardian bare-handed." (62)
- **Post-Script:** Unlock equipment UI tutorial (gear check).

---

### Event 05B: Gear Check (Upgrade Tease)
- **Trigger:** Optional interaction with a workbench (target cell)
- **Condition:** `flag_mage_joined && !flag_gear_check`
- **Script:**
    - **MAGE:** "You're using Type-2 plating – that's ceremonial-grade. It won't stop a proper bolt." (86)
    - **VAN:** "It stops blades. That's all I need." (36)
    - **MAGE:** "The Pit will eat you alive if you go in with that. We'll need a forge before Crystal Peaks." (84)
    - **VAN:** "Noted. Now focus on the door." (28)
- **Post-Script:** `flag_gear_check = true` (gameplay: hint at upgrading system).

---

### Event 05C: The Guard's Awakening (Party Expansion – Tense)
- **Trigger:** Spatial bounding box in corridor
- **Condition:** `flag_midboss_defeated && !flag_guard_awakened`
- **Script:**
    - **SYSTEM:** "MOTION DETECTED. UNREGISTERED RESONANCE. RESPONSE: NEUTRALIZE." (64)
    - **MAGE:** "It's a security construct! Van, don't let it—" (51)
    - *(Engine: Van parries, shield spark)*
    - **VAN:** "It's not attacking me. It's attacking you." (43)
    - **MAGE:** "What? I'm not Resonant!" (28)
    - **GRD:** "MAGE RESONANCE: NEGATIVE. DESIGNATION: NON-THREAT." (55)
    - **GRD:** "VAN RESONANCE: POSITIVE. DESIGNATION: PRIMARY VESSEL." (55)
    - **MAGE:** "...You're Resonant? You knew?" (34)
    - **VAN:** "Knew? I was trying to blow the door open. I didn't know I was a battery." (68)
    - **MAGE:** "This changes everything. You could have told me." (47)
    - **VAN:** "And you would have believed me? You're a researcher – you'd have tried to dissect me." (78)
    - **MAGE:** "…Fair point. But we're partners now, whether I like it or not." (53)
    - **GRD:** "VESSEL DETECTED. AWAITING DIRECTIVES." (40)
    - **VAN:** "Follow us. Stay silent. Don't shoot the wizard." (51)
    - **GRD:** "ACKNOWLEDGED." (12)
- **Post-Script:** `flag_guard_awakened = true`. Unlock party switching (playable 3‑member team for rest of slice).

---

### Event 06: Sanctum Terminal – Log 1 (Lore)
- **Trigger:** Player action facing terminal cell
- **Condition:** `!flag_terminal_1 && !flag_boss_dead`
- **Script:**
    - **SYSTEM:** "LOG-07: Resonance cascade detected. Primary containment failing. Recommend evacuation of Sector 4." (102)
    - **MAGE:** "Sector 4 – that's where we're standing." (39)
    - **VAN:** "So this was a research facility." (31)
    - **MAGE:** "Was. Now it's a tomb." (23)
- **Post-Script:** `flag_terminal_1 = true`

---

### Event 06B: Sanctum Terminal – Log 2 (Darker Truth)
- **Trigger:** Re-approach terminal after midboss (or after certain progress)
- **Condition:** `flag_midboss_defeated && !flag_terminal_2 && !flag_boss_dead`
- **Script:**
    - **SYSTEM:** "LOG-14: Subject 7 Resonance spiking. Uncontrolled release expected within 72 hours. Recommend termination." (115)
    - **VAN:** "Subject 7." (11)
    - **MAGE:** "Don't. You're not a subject – you're a person." (46)
    - **VAN:** "I'm a person who's about to explode. And you're still here." (52)
    - **MAGE:** "I'm here because I want to understand. And maybe help." (49)
    - **VAN:** "Help? You just met me." (22)
    - **MAGE:** "I've been studying Resonance for ten years. You're the first living sample that didn't burn out. That's worth more than any vault." (112)
- **Post-Script:** `flag_terminal_2 = true`

---

### Event 07: The Vault (Climax – OPR Betrayal)
- **Trigger:** Player action on the Relic tile
- **Condition:** `!flag_boss_dead`
- **Script:**
    - **MAGE:** "Class-7 containment unit. The Relic is its core processor." (66)
    - **VAN:** "Looks heavy. Stand back." (25)
    - *(Engine: screen shake, Van kneels, Relic embedded)*
    - **OPR:** "I figured you'd get greedy, Van." (34)
    - **VAN:** "OPR. You're supposed to be a handler, not a babysitter." (55)
    - **OPR:** "Contract's closed. Client wants the Relic and no loose ends." (65)
    - **VAN:** "I told him I don't work with a babysitter." (44)
    - **OPR:** "You're not the payload, Van – you're the *delivery method*." (60)
    - **VAN:** "What are you—" (16)
    - **GRD:** "PRIMARY CORE OFFLINE. SECONDARY DIRECTIVE: PROTECT RESONANCE VESSEL." (68)
    - **OPR:** "Scrap it all. Kill them!" (25)
- **Post-Script:** Boss encounter – Force Van Transformation, add GRD to party (if not already), OPR escapes (set `flag_opr_escaped = true`).

---

### Event 07B: Post-Boss – The Realization
- **Trigger:** Automated after combat
- **Condition:** `flag_boss_defeated`
- **Script:**
    - **MAGE:** "You absorbed raw Resonance and didn't detonate. Statistically impossible." (78)
    - **VAN:** "It wants out. I can feel it." (28)
    - **GRD:** "VESSEL STABILITY: 34%. RECALIBRATION REQUIRED. SEEK ADDITIONAL NODES." (74)
    - **VAN:** "Nodes? More of these things?" (31)
    - **MAGE:** "Six. Scattered across the continent. If OPR's client finds them first…" (77)
    - **VAN:** "Then we'd better get there first." (32)
    - **MAGE:** "We? You're still trusting me?" (29)
    - **VAN:** "Trust? No. But you know things I don't. That makes you useful." (62)
    - **MAGE:** "Likewise. I need a live specimen to study." (41)
    - **VAN:** "I'm not a specimen." (19)
    - **MAGE:** "Fine. A partner. For now." (23)
- **Post-Script:** Fade to black, transition to outro.

---

### Event 08: The New Contract (Expanded Outro)
- **Trigger:** Post-fade scene
- **Condition:** `flag_boss_defeated`
- **Script:**
    - **GRD:** "NODE LOCATIONS: CRYSTAL PEAKS, SUNKEN CITY, ASHEN FOREST, FORGOTTEN COAST, IRON KEEP, THE PIT." (106)
    - **VAN:** "The Pit. People don't come back from there." (44)
    - **MAGE:** "Good thing you're not people anymore." (39)
    - **VAN:** "…Point. Where's the nearest?" (32)
    - **GRD:** "CRYSTAL PEAKS. DISTANCE: 3 DAYS. RECOMMEND PREPARATION." (56)
    - **MAGE:** "Three days. That's a lot of walking – and a lot of time for OPR to catch up." (79)
    - **VAN:** "Then we'd better get started. One node at a time." (52)
    - **MAGE:** "And when we find them? What then?" (39)
    - **VAN:** "Then we figure out how to keep me from exploding. And maybe find out who's pulling OPR's strings." (89)
    - **MAGE:** "You think it's just one client?" (32)
    - **VAN:** "I think it's bigger than both of us. And that's exactly why we need to stay ahead." (77)
    - **MAGE:** "For once, I agree." (18)
    - **VAN:** "Let's go to work." (15)
- **Post-Script:** Fade to black. Roll credits. Show map with Crystal Peaks marked.

---

## IV. Optional Interactables (World-Building Flavor)

### Optional A: Broken Terminal (Scrap Reward)
- **Trigger:** Facing a damaged terminal
- **Condition:** `flag_mage_joined`
- **Script:**
    - **SYSTEM:** "ERR: MEMORY CORRUPTED. LAST FRAGMENT: '…do not trust the ones who…'" (72)
    - **MAGE:** "Looks like someone tried to erase the logs. Interesting." (50)
    - **VAN:** "Or they were hiding something." (30)
- **Post-Script:** Gain 3 scrap (hidden loot).

### Optional B: Faded Mural (Lore)
- **Trigger:** Facing a wall mural
- **Condition:** `flag_mage_joined`
- **Script:**
    - **MAGE:** "This mural depicts the First Resonance – when humans first bonded with the Nodes." (81)
    - **VAN:** "Bonded? You mean they volunteered?" (36)
    - **MAGE:** "Some did. Others… were chosen. The mural doesn't say how." (57)
    - **VAN:** "Chosen. Right. That's one word for it." (36)
- **Post-Script:** Lore flag.

---

## V. Character Arc Summary

| Event | Van ↔ Mage Dynamic |
|-------|---------------------|
| **02** | Pragmatic alliance; mutual distrust |
| **03** | Mage lectures; Van dismisses |
| **04** | Bickering but functional teamwork |
| **04B** | Tense cooperation under pressure |
| **04C** | Grudging acknowledgment |
| **05B** | Mage nitpicks; Van ignores |
| **05C** | Shock and suspicion (Resonance reveal) |
| **06B** | Vulnerable moment – Mage shows empathy |
| **07** | Unified against OPR |
| **07B** | Cautious partnership, not friendship |
| **08** | Shared goal – still pragmatic, but respectful |

---

## VI. Technical Summary

| Section | Approx. Bytes |
|---------|---------------|
| Act I (Events 01–04C) | 1,050 |
| Act II (Events 05–06B) | 1,100 |
| Act III (Events 07–08) | 900 |
| Optional interactables | 300 |
| **Total** | **~3,350 bytes** |

**Comfortably under 1.5% of 256KB resource budget.**  
The additional interactions and slower pacing transform the slice into a dense, replayable vertical slice that showcases the full party, hints at systems, and leaves players hungry for the full journey.