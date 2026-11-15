---
version: alpha
name: Revisit
description: Narrated Landsat films of how a place changed since 1984 — orbital, documentary, instrument-quiet.
colors:
  ground: "#0A0E13"
  surface: "#14181E"
  elevated: "#1C2229"
  ink: "#F4F1EC"
  muted: "#A8A49C"
  faint: "#7A766E"
  signal: "#5BA8A0"
  warn: "#C4A574"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: clamp(3rem, 8vw, 4.5rem)
    fontWeight: 500
    letterSpacing: -0.02em
  body:
    fontFamily: Manrope
    fontSize: 1rem
    lineHeight: 1.55
  ui:
    fontFamily: Space Grotesk
    fontSize: 1rem
    fontWeight: 600
  telemetry:
    fontFamily: IBM Plex Mono
    fontSize: 0.75rem
rounded:
  sm: 0.375rem
  md: 0.75rem
  control: 9999px
spacing:
  sm: 0.5rem
  md: 1rem
  lg: 2rem
components:
  button-primary:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.ground}"
    rounded: "{rounded.control}"
    padding: 0.625rem 1.25rem
  panel:
    backgroundColor: "{colors.surface}"
    border: "1px solid rgba(244,241,236,0.1)"
    rounded: "{rounded.md}"
---

## Overview

Revisit turns a place name into a 60–90s narrated film from real Landsat summer composites since 1984. The look comes from orbital imagery and documentary credits: deep space ground, warm paper ink, and one muted teal-cyan accent taken from true-color water — not SaaS blue, not acid green.

## Colors

- **Ground (#0A0E13):** Near-black orbital void; the globe reads against it.
- **Surface (#14181E) / Elevated (#1C2229):** Opaque instrument bezels and photo mounts over the globe — no frosted glass.
- **Ink (#F4F1EC):** Warm paper-white primary text (≥ AA on ground).
- **Muted (#A8A49C) / Faint (#7A766E):** Secondary and quiet labels (still readable).
- **Signal (#5BA8A0):** Single accent job — primary actions, focus rings, selected state, chart series.
- **Warn (#C4A574):** Semantic only — partial status, cause unknown, local-scope notes. Not a second brand accent.

## Typography

- **Display (Playfair):** Brand wordmark and year numerals over the film frame (the signature).
- **UI (Space Grotesk):** Place names, section titles, control labels in sentence case.
- **Body (Manrope):** Supporting copy and evidence prose.
- **Telemetry (IBM Plex Mono):** Years, clocks, metric tick values — not section eyebrows.

Do not default to tracked ALL-CAPS mono labels for chrome.

## Layout

- Landing: full-bleed globe hero; brand top-left; place picker + place card as a side stack.
- Story: player-dominant; metrics and evidence below; about/data as a quiet strip.
- Build: narrow progress column, same tokens as story.

## Elevation & Depth

Panels are opaque or lightly translucent solids. Reserve blur only if a surface truly floats over scrolling content — not on landing chrome over the globe. Film player uses a thin rebate edge (double hairline) as the signature craft detail.

## Shapes

Controls stay pill-shaped for hit targets. Panels use modest radius (`md`), not soft card kits everywhere. Imagery thumbs use `sm`–`md` mounts.

## Components

Shared: `Footer`, primary pill button (signal fill), opaque `panel` surface, sentence-case section titles in `font-ui`, focus-visible ring in signal.

## Do's and Don'ts

- Do keep the 3D globe as the landing hero; polish chrome around it, never gut it.
- Do reuse these tokens on `/`, `/story/[id]`, and `/build/[id]`.
- Do show story claims, script, and verified excerpts verbatim.
- Don't change: distinctive loaded faces (Playfair / Space Grotesk / Manrope / IBM Plex); globe / R3F / hotspots.
- Don't use: `#2997ff` SaaS blue, purple gradients, cream+terracotta cluster, acid-green-on-black, tracked ALL-CAPS mono as default chrome, reflexive `backdrop-blur` glass cards, arrow glyphs stapled to CTAs.
