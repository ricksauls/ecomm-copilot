# Handoff: ecomm-copilot web application

## Overview

ecomm-copilot is an agency-facing retail intelligence and creative production platform. Agencies manage a portfolio of client brands and their Walmart product listings, run competitive PDP (product detail page) analyses that produce a 0–100 score, and purchase creative deliverables (retail-ready image sets, seasonal versions, content optimization) that close the gaps the analysis identifies.

The product's core loop: **see which products are losing ground → understand why → buy the specific deliverable that fixes it.** Every screen in this design serves some part of that loop.

Tagline: **Your eCommerce Team, Amplified.**

Seven screens are designed, across two surfaces.

**Public surface** (dark — `01-Landing-and-Login.dc.html`):

1. Marketing landing page
2. Sign-in page

**Authenticated workspace** (light — `02-Workspace.dc.html`):

3. Agency Dashboard (portfolio level)
4. Product Workspace (single product overview)
5. Competitive PDP Analysis (the score, dimension by dimension)
6. Creative Workspace (the delivered 8-image set)
7. Share of Shelf (search visibility over time)

Plus a cross-cutting **Client View** mode that strips all pricing and credit information so the workspace screens can be shown to a client in a meeting.

**The two surfaces are deliberately inverted from each other.** The public site is the design system as written — near-black canvas, graphite cards. The workspace inverts it to a light canvas because it is a dense, all-day tool. Both use the identical type scale, hairline treatment, label style, and accent rule, so they read as one brand. Keep the split unless the client asks otherwise.

---

## About the Design Files

The files in this bundle are **design references created in HTML**. They are prototypes that demonstrate the intended look, layout, hierarchy, and navigation behavior. **They are not production code to copy directly.**

`Ecomm Copilot.dc.html` is authored in a proprietary component format (a declarative template plus a small logic class) that runs in a design tool. It will open in a browser for visual reference, but its runtime (`support.js`) is not something to build on.

The task is to **recreate these designs in the target codebase's existing environment** — React, Vue, Svelte, Rails views, whatever is already there — using its established component patterns, routing, state management, and styling approach. If no codebase exists yet, choose the most appropriate stack for a data-dense internal-facing web application and implement the designs there.

Read the HTML for exact values; read this README for intent and for the values you'd otherwise have to reverse-engineer.

## Fidelity

**High-fidelity.** Colors, typography, spacing, borders, and hover states are final and should be reproduced faithfully. Layout proportions are intentional. Copy is final and should be used verbatim — it was written to demonstrate the product's voice (plain, numeric, analyst-toned; see *Voice & Copy Rules* below).

Two things are **not** final:

- **Imagery.** Every image in the design is a gray placeholder box (`#EDEDED` fill, `#E1E1E1` 1px border, 2px radius, with a tracked-uppercase label like "Main hero"). Real Walmart product photography goes in these slots. The interface is deliberately monochrome so product imagery supplies all the color.
- **Data.** All numbers, product names, brand names, and item numbers are realistic fabrications for a pet-water-fountain scenario. Treat them as fixture data.

---

## Design Tokens

The **public site uses the Rick Sauls Design System palette as written** (`#050505` page, `#121212` cards, `#F3F3F3` text). The table below documents the **authenticated workspace**, which inherits the same five colors *inverted for a light application*. The design system is dark-canvas editorial; a data-entry-heavy web app needs a light canvas, so the same five colors are re-cast: near-black moves to the navigation rail, soft-white becomes the page canvas, and pure white becomes the card surface.

### Color

| Token | Hex | Role in this app |
|---|---|---|
| `black` | `#050505` | Navigation rail background; primary button fill; primary text on light; filled data bars |
| `graphite` | `#121212` | Raised surfaces **inside the dark rail only** (brand switcher, credits card) |
| `faint-gray` | `#2A2A2A` | Hairlines and borders inside the dark rail; avatar chip fill |
| `mid-gray` | `#4A4A4A` | Secondary body text on light surfaces; secondary numeric values |
| `cool-gray` | `#8C8C8C` | Labels, captions, metadata, placeholder text, muted nav items |
| `canvas` | `#F3F3F3` | Page background; text color on the dark rail |
| `surface` | `#FFFFFF` | Card fill |
| `border` | `#E1E1E1` | Card borders; table header rules; strong dividers on light |
| `divider` | `#EFEFEF` | Table row dividers (lighter than `border`) |
| `placeholder` | `#EDEDED` | Image placeholder fill; empty track behind data bars |
| `control-border` | `#C9C9C9` | Secondary/outline button borders; chip borders |
| `row-hover` / `row-active` | `#FAFAFA` | Table row hover; the highlighted "this is you" row |
| `signal-red` | `#FF3B30` | **The only accent.** See rule below. |

**Accent rule — important.** Signal Red appears at most **once or twice per view**, and only on something load-bearing. Current uses:

- The 3px vertical mark on the active navigation item
- The 32×2px rule above a card's headline (used on "Seasonal window", "Largest opportunity", "Read", "Package")
- Exactly one data point: the largest competitive gap (`−17` on the dashboard, `−38` on the analysis screen), the competitor leader's marker line, the leader's trend line
- The `In review` status label (the one item needing attention)
- Hover color on quiet text links

If you find yourself adding a third red element to a view, remove the one that isn't load-bearing.

**Known ambiguity, flagged for you.** Red currently carries three meanings: *your deficit*, *the competitor ahead of you*, and *needs attention*. Red conventionally means "bad," so a leader line in red can read as a problem rather than a benchmark. If this becomes confusing in use, the smallest fix is: reserve red for deficits and attention only, and render the leader's marker in `#C9C9C9`. Do not introduce green — the design system permits no sixth color.

### Typography

**Inter only**, self-hosted, four weights. The `.woff2` files ship in `fonts/` in this bundle. No CDN — the app must render correctly offline.

| Weight | File |
|---|---|
| 400 Regular | `fonts/Inter-Regular.woff2` |
| 500 Medium | `fonts/Inter-Medium.woff2` |
| 600 SemiBold | `fonts/Inter-SemiBold.woff2` |
| 700 Bold | `fonts/Inter-Bold.woff2` |

Fallback stack: `Inter, system-ui, sans-serif`. `-webkit-font-smoothing: antialiased` on body.

Type scale as used (size / weight / letter-spacing):

| Role | Size | Weight | Tracking | Notes |
|---|---|---|---|---|
| Screen title | 32px | 700 | −0.03em | line-height 1.1 |
| Product title (workspace) | 30px | 700 | −0.03em | line-height 1.15 |
| Hero metric (analysis) | 96px | 700 | −0.05em | line-height 0.8 |
| Large metric (product score) | 72px | 700 | −0.04em | line-height 0.85 |
| KPI metric | 40px | 700 | −0.03em | line-height 1 |
| Small metric | 34px / 26px | 700 | −0.03em | projected score; credits balance |
| Card headline | 15px | 600 | −0.01em | |
| Callout headline | 19px / 17px / 16px | 600 | −0.02em | line-height 1.25–1.3 |
| Body | 13px | 400 | 0 | color `#4A4A4A` |
| Table cell | 13.5px | 400/500 | 0 | |
| Secondary body / caption | 12.5px | 400 | 0 | |
| Metadata | 12px / 11.5px | 400 | 0 | color `#8C8C8C` |
| **Label** | **9.5px** | **500** | **+0.11em** | **UPPERCASE**, color `#8C8C8C` |
| Status pill | 11px | 500 | +0.06em | UPPERCASE |
| Section numeral | 12px | 600 | +0.06em | `01`, `02`, `03` |
| Button | 12.5px | 500 | 0 | |
| Nav item | 13px | 500 | 0 | |
| Brand wordmark | 15px | 700 | −0.02em | |
| Rail tagline | 9.5px | 500 | +0.11em | UPPERCASE |

Base body: 14px / line-height 1.5. **Never scale body text up for emphasis** — density is editorial. Emphasis comes from weight and from the label/metric contrast.

The **9.5px tracked uppercase label** is the single most characteristic element of this system. Nearly every card and table column begins with one. Get this right and the rest follows.

Casing: **sentence case everywhere** except labels, status pills, and section markers, which are uppercase and tracked. No ALL CAPS for emphasis in body copy.

### Spacing

4px base scale. Values in active use: `3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 19, 20, 22, 24, 26, 28, 30, 34, 52`.

Recurring decisions:

- **Card gap in all grids: `14px`.** Uniform, no exceptions.
- **Card padding: `20px 22px`** (compact/sidebar cards), **`24px`** (standard), **`26px`–`30px 34px`** (feature cards).
- **Page padding: `34px`** left/right, `34px` top, `60px` bottom (breathing room at scroll end).
- **Table row padding: `14px`–`19px` vertical, `22px`–`30px` horizontal.**
- **Label-to-content gap: `12px`–`16px`.**
- **Metric top margin: `14px`** below its label.

### Borders, Radii, Shadows

- **Cards and buttons: `4px` radius.** Image placeholders and chips: `2px`. Nothing is more rounded than 4px. No pills.
- **All borders 1px.** `#E1E1E1` on light surfaces, `#2A2A2A` inside the dark rail. Hover on outline controls bumps the border to `#050505` (light) or `#4A4A4A` (dark).
- **No drop shadows anywhere.** The `#F3F3F3` canvas against `#FFFFFF` cards plus a 1px `#E1E1E1` border does all the elevation work. This is deliberate and load-bearing — do not add shadows to "improve" card separation.
- **No left-border accent cards.** When a card needs an accent, it is a 32×2px red rule *above* the headline.

### Scrollbars

Custom: 10px, thumb `#D6D6D6`, square (0 radius), transparent track.

### Motion

The design is static, but the design system prescribes: fades of **140–220ms**, easing `cubic-bezier(0.22, 0.61, 0.36, 1)`. Hover states are **color/border shifts only — never scale.** Press state is a one-frame opacity dip to `0.85`. No zoom, no bounce, no slide-in.

---

## Global Layout (authenticated workspace)

This section describes the **workspace** shell only. The public site has its own layout, documented under *Screens — Public Surface*.

Full-viewport two-column shell, `height: 100vh`, `overflow: hidden`. Only the content pane scrolls.

```
┌──────────────┬─────────────────────────────────────────────┐
│              │  Topbar  60px, border-bottom #E1E1E1        │
│   Nav rail   ├─────────────────────────────────────────────┤
│   236px      │                                             │
│   #050505    │  Content pane — scrolls, padding 34px       │
│   fixed      │  inner max-width 1220px                     │
│              │                                             │
└──────────────┴─────────────────────────────────────────────┘
```

### Navigation Rail (236px, fixed, `#050505`)

Vertical flex, `padding: 28px 0 20px`. Top to bottom:

1. **Brand block** (`padding: 0 24px 26px`)
   - "ecomm-copilot" — 15px/700/−0.02em, `#F3F3F3`
   - 32×2px `#FF3B30` rule, `9px` margin above and below
   - "Your eCommerce Team, Amplified." — 9.5px/500/+0.11em uppercase, `#8C8C8C`

2. **Brand switcher** (`margin: 0 16px 22px`, `padding: 11px 13px`)
   `#121212` fill, 1px `#2A2A2A` border, 4px radius. Hover border → `#4A4A4A`. Contains: 22px square initials chip (1px `#4A4A4A` border, 2px radius, 9px/600 text), then a two-line block (brand name 12.5px/500 with ellipsis overflow; product count 10px `#8C8C8C`), then a 10px chevron SVG in `#8C8C8C` at 1.2px stroke. **This is a dropdown** — clicking it should open a brand/client picker. Not built in the design.

3. **Nav items** (`padding: 0 12px`, `gap: 1px`)
   Six items: Dashboard, Products, Competitive, Creative, Share of shelf, Billing.
   Each row: `padding: 9px 12px`, 4px radius, 13px/500, `display:flex; align-items:center; gap:10px`. First child is a **3px × 14px vertical mark** (1px radius).
   - Inactive: text `#8C8C8C`, background transparent, mark transparent
   - Active: text `#F3F3F3`, background `#121212`, mark `#FF3B30`

4. **Spacer** (`flex: 1`)

5. **Credits card** (`margin: 0 16px 14px`, `padding: 14px 14px 13px`)
   `#121212` fill, 1px `#2A2A2A` border, 4px radius.
   - Label "Available credits" (9.5px tracked uppercase, `#8C8C8C`)
   - Balance `4,250` — 26px/700/−0.03em — with `≈ $4,250` at 10.5px `#8C8C8C` on the same baseline
   - 1px `#2A2A2A` divider, then "Add credits" 12px/500, hover → `#FF3B30`

   Per the brief, credits read as **operational convenience, not virtual currency** — hence the plain `≈ $4,250` equivalence and the absence of coin/gem iconography. **Hidden entirely in Client View.**

6. **User block — this is the sign-out target** (`margin: 0 12px`, `padding: 8px 12px`, 4px radius, `cursor: pointer`, hover `background: #121212`). Contains a 24px `#2A2A2A` initials chip (2px radius), then "Dana Kirsch · Meridian" at 11.5px `#8C8C8C` (`flex:1`, ellipsis overflow), then the word "Sign out" at 10px/500/+0.09em uppercase `#4A4A4A`.

   The workspace takes a `signOut` callback and fires it from this row. In production this should almost certainly become a small menu (Account, Billing, Sign out) rather than a single-action row — the design shows the affordance, not the final interaction.

### Topbar (60px)

`padding: 0 34px`, background `#F3F3F3`, `border-bottom: 1px solid #E1E1E1`. Left: breadcrumb, 12.5px `#8C8C8C`, slash-separated. Then `flex:1` spacer. Then:

- **Search** — 250px wide, `padding: 7px 12px`, white fill, 1px `#E1E1E1`, 4px radius, 12px magnifier SVG (`#8C8C8C`, 1.3px stroke) + "Search products, brands" at 12.5px `#8C8C8C`. Non-functional in the design; implement as a real product/brand search.
- **Client View toggle** — outline button, `padding: 7px 13px`, 1px `#C9C9C9`, hover border `#050505`. Label toggles between "Client view" and "Exit client view".

Breadcrumb strings per screen:

| Screen | Breadcrumb |
|---|---|
| Dashboard | `Meridian Commerce Group` |
| Product | `Northlane Home / Cascade 84 oz Pet Water Fountain` |
| Analysis | `Northlane Home / Cascade 84 oz / Competitive analysis` |
| Creative | `Northlane Home / Cascade 84 oz / Creative` |
| Share of shelf | `Northlane Home / Pet Fountains / Share of shelf` |

### Page header pattern (every screen)

Repeats identically across all five screens:

```
flex, align-items: flex-end, justify-content: space-between, gap 24px, margin-bottom 26px
├─ left:  9.5px tracked uppercase eyebrow (#8C8C8C, margin-bottom 9px)
│         32px/700/−0.03em title (line-height 1.1)
│         13.5px #4A4A4A subtitle (margin-top 8px)
└─ right: button group, gap 9px
          secondary: padding 9px 15px, 1px #C9C9C9, white fill, 4px radius
          primary:   padding 9px 15px, #050505 fill, #F3F3F3 text, hover #2A2A2A
```

### Buttons

| Variant | Spec |
|---|---|
| Primary | `#050505` fill, `#F3F3F3` text, 12.5px/500, `padding: 9px 15px` (header) or `10px 14px` (in-card, full width, centered), 4px radius, hover `#2A2A2A` |
| Secondary | White fill, 1px `#C9C9C9`, `#050505` text, same padding/radius, hover border `#050505` |
| Tertiary (in-card action) | Same as secondary at `padding: 8px 13px` / 12px text |
| Quiet link | 12.5px/500 text, no chrome, often with a trailing `→`, hover `#FF3B30` |
| Chip | 12px, `padding: 6px 11px`, 1px `#C9C9C9`, **2px** radius, hover border `#050505` |

---

## Screens — Public Surface

The public surface is **dark**: `#050505` page, `#121212` cards, 1px `#2A2A2A` hairlines, `#F3F3F3` primary text, `#8C8C8C` secondary, Signal Red as the only accent. This is the Rick Sauls design system applied as written, no inversion.

Two conventions differ slightly from the workspace, because the public surface is display type rather than dense UI:

- **Labels are 10px / 500 / +0.12em uppercase** `#8C8C8C` (the workspace uses 9.5px / +0.11em).
- **The primary button is inverted**: `#F3F3F3` fill with `#050505` text, hover `#FFFFFF`. On the light workspace the primary button is the reverse. Secondary is a 1px `#2A2A2A` outline, hover `#4A4A4A`.

Section order: sticky header, hero, **why this matters**, how it works, deliverables & pricing, closing CTA, footer.

The nav has two items, not three — an earlier "Pricing" item duplicated the deliverables target, since that section is the price list. The section's own eyebrow still reads "Deliverables".

Content width is **1180px max, 40px side padding**, centered. Every section is separated by a full-bleed `border-top: 1px solid #2A2A2A` and carries `88px` vertical padding — no other divider treatment, no background changes between sections.

The arrow glyph on CTAs is the design system's own, taken verbatim from `ui_kits/website/Header.jsx`: a 15px `viewBox="0 0 16 16"`, `fill="none"`, `stroke="currentColor"`, `stroke-width="1.5"`, path `M3 8h10M9 4l4 4-4 4`.

---

### A. Landing Page

**Purpose.** Explain the product to an agency principal or brand e-commerce lead in one scroll, and route them to access.

**Sticky header** (68px). `position: sticky; top: 0`, `background: rgba(5,5,5,0.72)`, `backdrop-filter: blur(12px) saturate(140%)`, `border-bottom: 1px solid #2A2A2A`. This blur-over-translucent-black is the design system's sanctioned header treatment — one of only two places transparency is allowed.

Left: "ecomm-copilot" wordmark, 15px/700/−0.02em. Then nav at 13px/500 `#8C8C8C`, gap 26px, hover `#F3F3F3`: **How it works · Deliverables & Pricing**. Both scroll to their section with an **80px offset** so the 68px sticky header does not cover the heading. Implement as real anchors (`#how-it-works`, `#deliverables`) with `scroll-margin-top: 80px` rather than JS scrolling. Then `flex:1`. Right: "Sign in" as quiet text, then "Request access" as the inverted primary button (`padding: 9px 15px`).

**Hero** (`padding: 96px 40px 104px`). A single left-aligned column, `max-width: 820px`:

- Label "For retail agencies and brand teams"
- 32×2px `#FF3B30` rule (`margin: 20px 0 24px`)
- **H1: "Your eCommerce Team, Amplified."** — 60px/700/−0.035em, line-height 1.04, `text-wrap: pretty`
- Sub: "See which listings are losing ground on Walmart, understand exactly why, and buy the specific solution that closes the gap." — 17px `#8C8C8C`, `max-width: 520px`
- CTA row (`gap: 12px`, `margin-top: 36px`): "Request access" (primary, `padding: 13px 20px`, with the arrow glyph) and "Sign in to the workspace" (secondary)

The hero is deliberately typographic — no screenshot, no illustration, no product mock.

**Why this matters band** (`padding: 84px 40px 88px`) — the section immediately below the hero, and the client's stated reason the product exists. It is the loudest element on the page after the H1, by intent.

- Eyebrow "Why this matters" — 10px/500/+0.12em uppercase, and **rendered in `#FF3B30`** rather than `#8C8C8C`. This is the section's one red element.
- Headline "Driving discoverability and conversion." — **52px/700/−0.035em**, line-height 1.06, `max-width: 900px`. Second-largest type on the site (the H1 is 60px); the gap is deliberate and should be preserved if either size changes.
- Then `grid-template-columns: 1fr 1px 1fr`, `gap: 56px`, `margin-top: 56px`, `align-items: flex-start` — two equal columns split by a **1px `#2A2A2A` vertical divider** (`align-self: stretch`), the design system's split-and-divider motif. Each column: heading 26px/600/−0.025em, body 17px `#8C8C8C` at `margin-top: 14px`.

| Column | Heading | Body |
|---|---|---|
| Left | Discoverability | Getting your product in front of shoppers' eyes. |
| Right | Conversion | Converting those shoppers to customers. |

No cards, no icons, no background change — the section gets its weight from type scale and whitespace alone. If this needs to be more prominent later, **the lever is type scale, not color or a panel**: the design system permits no background other than `#050505` and no second accent.

**How it works** (`padding: 88px 40px`). `grid-template-columns: 340px 1fr`, `gap: 72px`, `align-items: flex-start`.

Left rail: label "How it works" / 32px/700/−0.03em headline "Four steps, and the fourth one closes the loop." / 13.5px `#8C8C8C` sub "Every recommendation names the gap it closes and the score it should move. Nothing is sold on a hunch."

Right: four numbered rows, each `display:grid; grid-template-columns: 56px 1fr; gap: 24px; padding: 26px 0; border-top: 1px solid #2A2A2A`. Numeral is 13px/600/+0.06em — **`01` is `#FF3B30`, `02`–`04` are `#8C8C8C`** (the accent rule: one red element per section). Title 18px/600/−0.015em; body 13.5px `#8C8C8C`, `max-width: 560px`.

| # | Title | Body |
|---|---|---|
| 01 | Add the listing | Point at a Walmart item number. Product assets and existing PDP content come in with it. |
| 02 | Score it | Six dimensions, measured against top competitors on the same shelf. |
| 03 | Buy the fix that closes the gap | Retail-ready image sets, content optimization, seasonal versions. |
| 04 | Watch the score and the shelf move | Share of shelf and search rank are tracked daily, so the next recommendation is based on what actually changed. |

**Deliverables** (`padding: 88px 40px`). Header row is `display:flex; align-items:flex-end; justify-content:space-between; gap:40px; margin-bottom:34px` — left: label "Deliverables" + 32px/700/−0.03em "Priced per output, not per seat."; right: 13px `#8C8C8C`, `max-width: 330px`, "Buy against credits or a card. Credits are an accounting convenience, not a currency."

Then `grid-template-columns: repeat(3, 1fr)`, `gap: 16px`. Six cards: `#121212` fill, 1px `#2A2A2A`, 4px radius, `padding: 24px`, `min-height: 196px`, `display:flex; flex-direction:column`, hover border `#4A4A4A`. Structure: kind label / name (17px/600/−0.015em) / body (13px `#8C8C8C`) / `flex:1` spacer / footer row (`margin-top:20px; padding-top:16px; border-top:1px solid #2A2A2A`, space-between) with price at 19px/700/−0.02em and turnaround at 11.5px `#4A4A4A`.

| Kind | Name | Body | Price | Turnaround |
|---|---|---|---|---|
| Creative | Retail-ready PDP image set | Eight images at 2000 × 2000, built to Walmart specification from your assets and approved claims. | $450 | 3 days |
| Creative | Seasonal version | Reuses approved creative for a matched occasion. | $180 | 2 days |
| Content | Content optimization | Title, bullets, and description rewritten to fit Walmart recommended format as well as optimizing for search. | $275 | 2 days |
| Analysis | Competitive PDP analysis | The 0–100 score, decomposed across six dimensions, with the ranked actions that move it. | $350 | 2 days |
| Monitoring | Competitive monitoring | Share of shelf, search rank, availability, and price position across your tracked terms. | $95 / mo | Daily |
| Portfolio | Agency workspace | Every brand and listing in one view, ranked by how much ground each one is losing. | Included | Per agency |

No card is visually promoted over the others — no "most popular" badge, no highlighted tier. Prices are stated and left alone. **Note there is no red in this section**; the accent budget was spent on the numerals above.

**Closing CTA** (`padding: 88px 40px 96px`). `display:flex; align-items:flex-end; justify-content:space-between; gap:52px`. Left: 38px/700/−0.03em, `max-width: 620px`, "Bring in one portfolio and see where it stands this right now." Right: "Request access" primary with arrow, `padding: 14px 22px`, `flex: 0 0 auto`.

> **Copy flag.** "stands this right now" is the client's verbatim wording and reads like a leftover word from an earlier revision ("stands this week"). Confirm before launch; the likely intent is "…see where it stands right now."

**Footer** (`padding: 30px 40px`). Single row, `gap: 26px`: wordmark at 13px/700 / "Your eCommerce Team, Amplified." at 11.5px `#4A4A4A` / `flex:1` / links at 11.5px `#8C8C8C` (`gap: 22px`, hover `#F3F3F3`): Privacy · Terms · Sign in.

---

### B. Sign-in Page

**Purpose.** Get an existing user into the workspace, and hold the brand argument while they do it.

Full-viewport split, `height: 100vh`, no scroll.

**Left panel** (`flex: 1`, `border-right: 1px solid #2A2A2A`, `padding: 44px 56px`, vertical flex). Wordmark pinned top-left (clickable → landing page, `align-self: flex-start`). Then `flex:1` spacer, a centered block at `max-width: 480px`, another `flex:1`, and a footer line.

The centered block: 32×2px red rule / 34px/700/−0.03em "Every listing you manage, scored against the competition." / 14px `#8C8C8C` "Meridian Commerce Group holds 15 brands and 148 products in one portfolio view. Twenty-four analyses ran this month."

Footer line: 11.5px `#4A4A4A`, "Walmart · Amazon and Target next".

The left-panel copy is **fixture content standing in for a real value line.** It currently names the demo agency, which a real sign-in page would not do. Replace it with a fixed brand statement, or with genuine aggregate stats if you have them.

**Right panel** (`width: 520px; flex: 0 0 520px`, centered content, `padding: 44px 64px`):

- Label "Sign in" / "Welcome back" at 26px/700/−0.025em / 13px `#8C8C8C` "Use the address your agency was granted access on."
- **Work email** field (`margin-top: 32px`): 10px tracked label, then the input at `margin-top: 9px`, `#121212` fill, 1px `#2A2A2A`, 4px radius, `padding: 12px 14px`, 14px `#F3F3F3`, hover border `#4A4A4A`
- **Password** field (`margin-top: 20px`): label row is space-between with "Forgot?" at 11.5px `#8C8C8C`, hover `#FF3B30`. Input same spec; the masked value is rendered at `letter-spacing: 0.16em` in `#8C8C8C`
- "Sign in" primary button, full width, `padding: 13px 18px`, `margin-top: 26px`
- Divider (`margin: 26px 0`): two `flex:1` 1px `#2A2A2A` rules with "or" between them at 10px/500/+0.12em uppercase `#4A4A4A`
- "Continue with single sign-on" secondary button, full width
- Fine print at 12px `#4A4A4A`, `margin-top: 34px`: "Access is granted per agency. Request access if your team is not set up yet." — "Request access" is `#8C8C8C` and clickable

**The fields are static text, not real inputs.** They show the resting visual state only. Focus, filled, invalid, disabled, and loading states all need designing — and the design system's hover-only rule doesn't cover focus rings, so that's a genuine open decision. A 1px `#4A4A4A` border plus no glow would be the in-system answer.

SSO is shown as a single generic button rather than per-provider buttons. If the real product uses Google/Microsoft/Okta, that changes the layout and needs the vendors' own mark guidelines respected.

---

## Screens — Authenticated Workspace

Everything below this line is the **light** surface. See *Global Layout* above for the shell.

### 1. Agency Dashboard

**Purpose.** The agency's morning view: which client products are losing ground, what needs action, what shipped recently.

**Header.** Eyebrow "Portfolio" / title "Meridian Commerce Group" / subtitle "15 brands · 148 products · Walmart · week of Aug 10". Buttons: `Export report` (secondary), `Add product` (primary).

**KPI row.** `grid-template-columns: repeat(4, 1fr)`, gap 14px, margin-bottom 14px. Each card: white, 1px `#E1E1E1`, 4px radius, `padding: 20px 22px`. Structure = label / 40px metric / 12px `#8C8C8C` footnote.

1. Products managed — `148` — "+6 this month"
2. Avg competitive score — `71` with `+3` at 14px/500 `#4A4A4A` alongside — **plus a 7-bar sparkline** in place of a footnote: `display:flex; gap:2px; align-items:flex-end; height:16px`, each bar `flex:1`, heights `8,9,8,11,12,13,16`px, colors `#E1E1E1 ×4, #C9C9C9 ×2, #050505` (the final/current bar is black). A trend shown in three tones, no axes, no chart library.
3. Analyses run — `24` — "Aug · 8 pending review"
4. Creative sets delivered — `11` — "88 images · 3 in production"

**Main row.** `grid-template-columns: 1fr 340px`, gap 14px.

**Left: "Products losing ground" table.** Card with `padding: 22px 0 6px` (zero horizontal so rows can span edge to edge; rows carry their own `22px` inset).

- Card header (`padding: 0 22px 16px`): headline "Products losing ground" (15px/600), sub "Ranked by gap to the leading competitor in each set" (12.5px `#8C8C8C`), and right-aligned quiet link "All 148 →".
- Column grid, used by both header and rows: **`1fr 78px 96px 150px`**. Header row is 9.5px tracked uppercase with `border-bottom: 1px solid #E1E1E1`; columns 2–4 right-aligned. Headers: Product / Score / Gap to top / Recommended.
- Five rows, `padding: 15px 22px`, `border-bottom: 1px solid #EFEFEF` (last row has none), `hover: background #FAFAFA`, `cursor: pointer`.
  - **Product cell**: 34px square placeholder (`#EDEDED`, 1px `#E1E1E1`, 2px radius) + gap 12px + two lines (name 13.5px/500 with ellipsis; "Brand · #item" 11.5px `#8C8C8C`)
  - **Score**: 16px/600/−0.02em
  - **Gap to top**: 14px/600 — `#FF3B30` **only on the first (worst) row**, `#4A4A4A` on all others
  - **Recommended**: 12.5px/500 `#050505` with trailing `→`

  | Product | Brand · Item | Score | Gap | Recommended |
  |---|---|---|---|---|
  | Cascade 84 oz Pet Water Fountain | Northlane Home · #WM-4471902 | 72 | −17 | PDP image set → |
  | Harbor 6-Quart Enameled Dutch Oven | Fieldhouse Kitchen · #WM-2298140 | 64 | −22 | Run analysis → |
  | Trailmark Insulated 32 oz Tumbler | Trailmark Outdoors · #WM-8830271 | 69 | −15 | Tailgating set → |
  | Northlane Ceramic Slow Cooker, 7 qt | Northlane Home · #WM-5512088 | 58 | −28 | Content rewrite → |
  | Kestrel 20V Cordless Leaf Blower | Kestrel Tools · #WM-1094552 | 75 | −9 | Run analysis → |

  Note the sort: **not** by score, and **not** strictly by gap. It's an editorial "needs attention" ranking. If you implement real sorting, expose the ordering logic rather than silently sorting by one column.

**Right column** (flex, gap 14px):

- **Seasonal window card** (`padding: 20px 22px`): label "Seasonal window" / 32×2px red rule / headline "Back to School closes in 19 days" (17px/600/−0.02em, line-height 1.25) / body 13px `#4A4A4A` `text-wrap: pretty` / full-width primary button "Review 12 eligible products".
- **Recent activity card** (`padding: 20px 22px 8px`): label, then four items at `padding: 14px 0` with `1px #EFEFEF` bottom dividers (last has none). Each: title 13px/500, meta 11.5px `#8C8C8C` (3px gap).

**Navigation.** Row 1 of the table → Product Workspace.

---

### 2. Product Workspace

**Purpose.** Everything known about one product, and what to do next.

**Product header** — a distinct layout, not the standard page header. `display:flex; gap:26px; align-items:flex-start`:

- 148px square image placeholder (4px radius, centered "Main image" label)
- Center block: eyebrow "Northlane Home · Walmart" / title 30px/700/−0.03em / **four-stat row** (`gap:26px`, `margin-top:16px`), each = 9.5px tracked label + 16px/600 value with 5px gap:

  | Price | Rating | Item | Competitive set |
  |---|---|---|---|
  | $48.97 | 4.3 · 1,284 | #WM-4471902 | 7 products |

- Right block: 190px column, gap 9px — `Open latest analysis` (primary) and `View on Walmart` (secondary), both full width and centered.

**Tab bar.** `display:flex; gap:26px; border-bottom: 1px solid #E1E1E1; margin: 22px 0 26px`. Tabs: Overview, Competitive, Creative, Content, Share of shelf, History. Each `padding: 0 0 12px`, 13px/500, `margin-bottom: -1px` so the active underline sits on the container rule. Active: `#050505` text + 2px `#050505` bottom border. Inactive: `#8C8C8C`, transparent border.

Three tabs are wired to other screens: **Competitive → Analysis**, **Creative → Creative**, **Share of shelf → Shelf**. Overview, Content, and History set the tab state but have no content built — Content and History need designing before launch.

**Row 1** — `grid-template-columns: 1fr 1fr`, gap 14px, margin-bottom 14px:

- **Score card** (`padding: 24px`): label "Competitive PDP score". Then `display:flex; align-items:flex-end; gap:20px`: `72` at 72px/700/−0.04em, `/ 100` at 15px `#8C8C8C` (`padding-bottom: 6px`), spacer, then a right-aligned two-line reference block ("Category avg 64", "Top competitor 89" at 12.5px `#4A4A4A`).

  Below: a **6px track** (`#EDEDED`) with three absolutely-positioned children — a `#050505` fill at `width: 72%`, a 1px `#8C8C8C` tick at `left: 64%`, and a 1px `#FF3B30` tick at `left: 89%`. Both ticks overhang the track by 6px top and bottom (`top:-6px; bottom:-6px`). Beneath: `0` / "Analyzed Aug 14 · 7 competitors" / `100` at 11px `#8C8C8C`, space-between.

- **Asset profile card** (`padding: 24px`): label, then a `1fr 1fr` grid with `gap: 16px 22px`. Six entries, each = 13px/500 title + 12px `#8C8C8C` detail. The first four carry `padding-bottom: 14px; border-bottom: 1px solid #EFEFEF`; the last two don't.

  Product assets (14 files · updated Jul 30) · Brand assets (Logo, type, palette) · Features & benefits (9 approved) · Approved claims (4 on file) · Existing PDP content (6 images · 312-word copy) · Generated creative (1 set · 8 images)

  This card is the product's answer to "what do we have to work with" — it gates what creative can be produced.

**Row 2** — `grid-template-columns: 1fr 340px`, gap 14px:

- **"What this product needs next"** (`padding: 24px`): headline + sub "Ordered by the size of the competitive gap it closes", then three recommendation rows. Each row: `display:flex; align-items:flex-start; gap:18px; padding:18px 0; border-top: 1px solid #EFEFEF`.
  - Numeral `01`/`02`/`03` at 12px/600/+0.06em — **`01` is `#FF3B30`, the rest `#8C8C8C`** (`padding-top: 2px` for baseline alignment)
  - Middle (`flex:1`): 14px/500 finding + 12.5px `#4A4A4A` explanation, `text-wrap: pretty`
  - Right (`flex: 0 0 172px`, right-aligned): the CTA, then price at 11.5px `#8C8C8C` — **`01` gets a primary button, the rest secondary**

  | # | Finding | Detail | Action | Price |
  |---|---|---|---|---|
  | 01 | Lifestyle imagery trails the competitive set by 38 points | Six of seven competitors show the fountain in a real kitchen with a pet. This listing has none. | PDP image set (primary) | $450 · 450 credits |
  | 02 | Feature copy omits 4 of 9 approved benefits | Filtration life, quiet-pump rating, dishwasher-safe basin and BPA-free material appear in competitor bullets. | Content optimization | $275 · 275 credits |
  | 03 | Search rank slipped to #3 on "pet water fountain" | Share of shelf is rising, but two competitors hold the top slots on the highest-volume term. | Share of shelf | Included · monitored |

  This card is the commercial heart of the product. Note that item 03 sells nothing — it routes to already-included monitoring. Not every recommendation is an upsell, and that restraint is intentional.

- **Right column**: **Competitive set card** (`padding: 20px 22px 10px`) — label + four rows (`padding: 12px 0`, `1px #EFEFEF` dividers, last none), each = 28px placeholder + name (12.5px, `flex:1`) + score (13px/600). PetSpring Ultra Quiet 100 oz `89`, Vireo Steel Fountain, 96 oz `81`, Brookline Pet Waterer `74`, then "4 more in set" with "avg 61" in `#8C8C8C`.

  **Recommended occasions card** (`padding: 20px 22px`) — label + wrapping chip row (`gap: 7px`): Back to School, Holiday, Travel, Camping. Then 12px `#8C8C8C` note: "Matched to product type and category demand. Tailgating and Mother's Day were excluded as poor fits." **Stating the exclusions is deliberate** — it demonstrates the matching logic is real rather than decorative.

**Navigation.** "Open latest analysis" and recommendation 01's context → Analysis. PDP image set → Creative. Share of shelf → Shelf.

---

### 3. Competitive PDP Analysis

**Purpose.** The deliverable a client pays for: the score, decomposed, with the one action that closes the most ground.

**Header.** Eyebrow "Competitive PDP analysis · Aug 14" / title "Cascade 84 oz Pet Water Fountain" / sub "Northlane Home · measured against 7 competitors in Pet Fountains". Buttons: `Export PDF` (secondary), `Act on findings` (primary → Creative).

**Hero score card** (`padding: 30px 34px`, margin-bottom 14px). `display:flex; gap:52px; align-items:center`:

- Left: label / `96` at 96px/700/−0.05em with `/ 100` at 17px `#8C8C8C` / "Above category. 17 points from the leader." at 12.5px `#4A4A4A`, `margin-top: 16px`
- A **1px `#E1E1E1` vertical divider**, `align-self: stretch` — the design system's thin-vertical-divider motif
- Right (`flex:1`): a 130px-tall absolutely-positioned comparison plot
  - Baseline: 1px `#E1E1E1` at `top: 64px`, full width
  - Your bar: `#050505`, `width: 64%`, `height: 12px`, `top: 52px`
  - "Your product 72" label at `top: 24px`, 11px/500/+0.08em uppercase `#8C8C8C`
  - Category marker: 1px `#8C8C8C` vertical at `left: 64%`, `top:0; bottom:34px`; label "Category 64" at 11.5px `#4A4A4A`, `bottom: 12px`, `margin-left: -38px`
  - Leader marker: 1px `#FF3B30` at `left: 89%`, same vertical extent; label "Leader 89" at 11.5px/500 `#FF3B30`, `margin-left: -6px`
  - A 1px `#050505` tick at `left: 72%`, `top: 38px`, `height: 40px`

  The bar width (64%) and the category marker (64%) coinciding is a coincidence of this dataset, not a rule. Drive both from data.

**"Where the score comes from"** (`padding: 24px 0 10px`, margin-bottom 14px). Header inset `0 30px 18px`: headline + "Bar is this product. Thin marks are the category average and the leader."

Column grid **`210px 1fr 76px`**. Header row (`padding: 0 30px 10px`, `border-bottom: 1px solid #E1E1E1`): Dimension / (blank) / Gap to leader.

Six dimension rows, `padding: 19px 30px`, `border-bottom: 1px solid #EFEFEF` (last none). Each row:

- Dimension name, 13.5px/500 (600 on the highlighted row)
- **Bar cell** — `position: relative; height: 22px; padding-right: 34px` (the inset reserves room for the score). Children:
  - Empty track: `#F3F3F3`, `left:0; right:34px; top:9px; height:4px`
  - Value bar: `left:0; top:5px; height:12px`, width = score as % of track
  - Category tick: 1px `#8C8C8C`, full row height
  - Leader tick: 1px `#C9C9C9`, full row height
  - Score value: 13px/600, `right:0; top:3px`
- Gap to leader, 13.5px/600, right-aligned

| Dimension | Score | Bar width | Category tick | Leader tick | Gap |
|---|---|---|---|---|---|
| Visual merchandising | 61 | 57.9% | 60.5% | 89.3% | −33 |
| Benefit communication | 78 | 74.1% | 70.3% | 83.6% | −10 |
| **Lifestyle content** | **54** | **51.3%** | **75.1%** | **87.4%** | **−38** |
| Product-in-use | 70 | 66.5% | 64.6% | 79.8% | −14 |
| Content depth | 73 | 69.4% | 76.9% | 86.5% | −18 |
| Ratings & reviews | 84 | 79.8% | 68.4% | 87.4% | −8 |

**The Lifestyle content row is the emphasis row**, and it uses four separate signals at once: `background: #FAFAFA`, dimension name at weight 600, value bar in `#FF3B30` instead of `#050505`, and the gap at weight 700 in `#FF3B30`. This is the one place in the app where the accent does real analytical work — it marks the single largest gap, which is also what the primary CTA below sells. Widths are percentages of the track, so recompute them from data rather than hardcoding.

**Bottom row** — `1fr 1fr`, gap 14px:

- **"Largest opportunity"** (`padding: 26px`): label / 32×2px red rule / 19px/600/−0.02em headline "Lifestyle imagery is the single biggest source of the gap" / 13px `#4A4A4A` body ending in the projected outcome ("moves the composite score to an estimated 81") / then `display:flex; align-items:center; gap:14px` with the primary CTA "Create recommended PDP image set" (`padding: 11px 16px`) and "$450 · 3 business days" at 12.5px `#8C8C8C`.
- **"Also worth doing"** (`padding: 26px`): label, then three items at `padding: 14px 0` with `border-top: 1px solid #EFEFEF`, each = numeral (11.5px/600 `#8C8C8C`) + body (`flex:1`: 13.5px/500 title + 12px `#8C8C8C` "service · price") + right action.
  - `02` Add 4 approved benefits to bullets — Content optimization · $275 — "Add →"
  - `03` Close the price gap: −2% vs category — **"Recommendation only · no action needed"** — action is an em-dash `—`
  - `04` Monitor the set monthly — Competitive monitoring · $95 / month — "Add →"

  Then a footer: `margin-top:16px; padding-top:16px; border-top: 1px solid #E1E1E1`, space-between — "Selected: 1 item · $450" (13px/500) and `Review order` (secondary).

  Item 03 having nothing to sell is again deliberate. A recommendation engine that always has something to sell isn't trusted.

---

### 4. Creative Workspace

**Purpose.** Review, approve, and download the delivered 8-image PDP set; spin off seasonal versions.

**Header.** Eyebrow "Retail-ready PDP image set · delivered Aug 15" / title "Cascade Pet Fountain — core set" / sub "8 images · 2000 × 2000 · Walmart PDP specification · 9 approved claims applied". Buttons: `Share with client` (secondary), `Download set` (primary).

**Layout.** `grid-template-columns: 1fr 320px`, gap 14px. (Note: 320px here, not the 340px used elsewhere.)

**Left column** — four cards, gap 14px. Each has the same header pattern: `display:flex; align-items:baseline; justify-content:space-between; margin-bottom:16px` with a 9.5px tracked uppercase label on the left (numbered `01 ·`, `02 ·`, …) and an 11.5px `#8C8C8C` note on the right.

1. **`01 · Main image`** — note "White background · no overlay text". Body is `display:flex; gap:20px`: a **300px square** placeholder, then a `flex:1` column — 14px/500 caption "Front three-quarter, stainless finish, full 84 oz basin", 12.5px `#4A4A4A` rationale ("Cropped to Walmart's 85% fill guidance so the product reads at thumbnail size in search"), `flex:1` spacer, then a bottom-aligned action row (gap 8px): `Download` `Regenerate` `Edit` (tertiary) + `Approve` (primary). Per-image actions live on the image, not in a global toolbar.

2. **`02 · Lifestyle`** — note "2 images · closes the largest scoring gap" (ties creative back to the analysis). `1fr 1fr` grid, gap 14px. Each: **230px-tall** placeholder, then a caption row (`margin-top: 10px`, space-between): 12.5px/500 title + a **status pill** (11px/500/+0.06em uppercase, 1px `#E1E1E1`, 2px radius, `padding: 3px 7px`).
   - "In-kitchen with pet" — `Approved` (`#8C8C8C`)
   - "Evening living room" — `In review` (**`#FF3B30`**)

3. **`03 · Feature & benefit`** — note "2 images · drawn from approved claims". Same two-up 230px pattern. "3-stage filtration" and "24 dB quiet pump", both `Approved`.

4. **`04 · Product in use & scale`** — note "3 images". `repeat(3, 1fr)`, **190px-tall** placeholders, caption only (12.5px/500, `margin-top: 10px`), no status pills. "One-hand refill", "Dishwasher-safe basin", "Size & capacity".

The 300 → 230 → 230 → 190px height progression encodes importance: the main image is the one that appears in search results.

**Right column** — three cards, gap 14px:

- **Package** (`padding: 20px 22px`): label / 32×2px red rule / four spec rows (`padding: 8px 0`, 12.5px, space-between, `1px #EFEFEF` dividers, last none): Images `8 of 8`, Approved `7`, Revisions used `1 of 3`, Charged `450 credits` (**price row hidden in Client View**) / full-width primary `Approve remaining`.
- **Seasonal versions** (`padding: 20px 22px`): label / two-up thumbnails (`gap: 10px`, 74px tall) with 11.5px captions "Halloween · live" and "Holiday · draft" / 12.5px `#4A4A4A` explanation of reuse / "$180 per occasion · 2 days" (12px `#8C8C8C`, **hidden in Client View**) / full-width secondary `Create seasonal version`.
- **Expected impact** (`padding: 20px 22px`): label / `81` at 34px/700 with "projected score, from 72" at 13px `#4A4A4A` alongside / 12px `#8C8C8C` "Recalculated after the set goes live on the PDP."

  Closing the loop back to the score is the whole argument for the purchase — keep this card.

---

### 5. Share of Shelf

**Purpose.** Search visibility over time against the competitive set.

**Header.** Eyebrow "Share of shelf · 8 weeks" / title "Pet Fountains — Northlane Home" / sub "14 tracked terms · Walmart search · updated daily". Buttons: `8 weeks` and `Export PDF`, both secondary. (`8 weeks` should become a real range selector.)

**KPI row** — `repeat(4, 1fr)`, gap 14px, same card spec as the dashboard:

1. Share of shelf — `18.4%` with `+6.3` — "vs 12.1% eight weeks ago"
2. Search rank — `#3` with `+1` — "\"pet water fountain\""
3. Availability — `94%` — "2 stockouts in 8 weeks"
4. Price position — `−2%` — "vs category average $49.98"

**Trend chart card** (`padding: 26px 30px`, margin-bottom 14px). Header: headline "Share of shelf over time" + sub "Percent of first-page slots across 14 tracked terms" on the left; on the right a legend (`display:flex; gap:18px`, 11.5px `#4A4A4A`), each item = a 14×2px color swatch + label: **Cascade `#050505`**, **PetSpring `#FF3B30`**, **Category avg `#C9C9C9`**.

**Inline SVG, no chart library.** `viewBox="0 0 820 280"`, rendered at `width:100%; height:290px; overflow:visible`.

- Gridlines at `y = 40, 106, 173` in `#F0F0F0`; baseline at `y = 240` in `#E1E1E1`; all `x1=40 → x2=800`
- Y labels at `x=24`, `text-anchor="end"`, 10px `#8C8C8C`: 30%, 20%, 10%, 0
- X labels at `y=266`, 10px `#8C8C8C`: "Jun 22" (left), "Jul 20" (middle, centered), "Aug 16" (right, end-anchored)
- Three paths, drawn back to front: category `#C9C9C9` 1.5px, PetSpring `#FF3B30` 2px, Cascade `#050505` 2.5px. All `fill: none`.
- End-of-series annotations: a 4px `#050505` dot at `(780, 117.3)`; "18.4%" at 12px/600 `#050505`, `(780, 102)`, end-anchored; "22.9%" at 11px `#FF3B30`, `(780, 78)`, end-anchored
- All `<text>` needs an explicit `font-family="Inter"` — SVG text does not inherit the body font in every engine

**Path geometry.** Points map to `x = 40 + 740 · i/(n−1)` and `y = 240 − (value/30) · 200` — i.e. the y-axis is fixed to a 0–30% domain. The curves are **Catmull-Rom splines converted to cubic Béziers** with a 1/6 tension factor, endpoints duplicated. The conversion function is in the logic class of the source file; port it or substitute your charting library's monotone/cardinal spline. Series (8 weekly points each):

```
Cascade:      12.1, 13.4, 14.0, 15.2, 15.0, 16.8, 17.6, 18.4
PetSpring:    26.0, 25.4, 25.8, 24.9, 24.2, 24.0, 23.4, 22.9
Category avg:  9.0,  9.2,  9.5,  9.4,  9.8, 10.0, 10.2, 10.4
```

If you swap in a chart library, keep these constraints: no axis-line chrome beyond what's listed, no dots except the final one, no tooltips-as-decoration, no area fills, no legend box.

**Bottom row** — `1fr 340px`, gap 14px:

- **"Competitive set, this week"** (`padding: 22px 0 6px`): headline inset `0 24px 16px`. Column grid **`1fr 92px 92px 92px`**, header inset `0 24px 9px` with `1px #E1E1E1` bottom rule: Product / Share / Rank / Price (columns 2–4 right-aligned). Five rows, `padding: 14px 24px`, `1px #EFEFEF` dividers.

  | Product | Share | Rank | Price |
  |---|---|---|---|
  | PetSpring Ultra Quiet 100 oz | **22.9%** (`#FF3B30`, 600) | #1 | $54.88 |
  | Vireo Steel Fountain, 96 oz | 20.1% (600) | #2 | $46.50 |
  | **Cascade 84 oz — yours** (600) | 18.4% (600) | #3 (600) | $48.97 |
  | Brookline Pet Waterer | 11.7% (600) | #5 | $39.94 |
  | 4 others in set (`#8C8C8C`) | 26.9% (`#8C8C8C`) | — | — |

  Your own row is marked with `background: #FAFAFA`, weight 600 across all four cells, and the suffix "— yours". Rank #4 is absent — an untracked competitor holds it, and the design doesn't paper over the gap.

- **"Read" card** (`padding: 20px 22px`): label "Read" / 32×2px red rule / 16px/600/−0.02em headline "Share is up 6.3 points, but the top slot is holding" / 13px `#4A4A4A` body ("Its advantage is content, not price — it outscores this listing on lifestyle imagery by 38 points") / full-width primary `Open the PDP analysis`.

  Every data screen ends in a card like this: a plain-language read that names the cause and routes to the action. Do not replace it with an AI-summary treatment (sparkle icons, streaming text, "AI Insights" branding) — the value is the analysis, not the fact that software wrote it.

---

## Interactions & Behavior

### Navigation

All navigation is **client-side view switching** in the design. In a real app these become routes:

| From | Trigger | To |
|---|---|---|
| Landing page | "Request access" (header, hero, closing), "Sign in" (header, footer), "Sign in to the workspace" | Sign-in page |
| Sign-in page | "Sign in", "Continue with single sign-on" | Dashboard (authenticated) |
| Sign-in page | wordmark, "Request access" in the fine print | Landing page |
| Rail | user block at the bottom | signs out → Landing page |
| Rail | Dashboard / Products / Competitive / Creative / Share of shelf | corresponding screen |
| Rail | Billing | not built — currently routes to Dashboard |
| Dashboard | first table row | Product |
| Dashboard | "All 148 →" | product list — **not built** |
| Product | tab: Competitive / Creative / Share of shelf | Analysis / Creative / Shelf |
| Product | "Open latest analysis" | Analysis |
| Product | recommendation 01 "PDP image set" | Creative |
| Product | recommendation 03 "Share of shelf" | Shelf |
| Analysis | "Act on findings", "Create recommended PDP image set" | Creative |
| Shelf | "Open the PDP analysis" | Analysis |
| Topbar | Client view toggle | toggles mode in place |

Suggested routes — public: `/`, `/signin`. Authenticated: `/app`, `/app/products`, `/app/products/:id`, `/app/products/:id/analysis`, `/app/products/:id/creative`, `/app/products/:id/shelf`, `/app/billing`.

Note that navigation also sets the product tab, so arriving at Analysis from anywhere leaves the product's tab bar showing "Competitive" selected. Keep tab state derived from the route rather than stored separately.

### Client View

A single boolean with three effects:

1. Credits card in the rail is **removed** (not disabled)
2. All prices, credit costs, and "Charged" rows are **removed** — `$450 · 450 credits`, `$275 · 275 credits`, `$450 · 3 business days`, `Charged 450 credits`, `$180 per occasion · 2 days`, `Selected: 1 item · $450`
3. Toggle label becomes "Exit client view"

Purchase CTAs themselves stay visible — the client should see what's recommended, just not what the agency pays. This is the design's answer to "would you put this on a conference-room screen in front of the client?" Worth persisting per user, and worth considering as a shareable read-only link.

### Hover states

Only two kinds, both color-only, never scale:

- Table rows: `background → #FAFAFA`
- Outline controls: `border-color → #050505` (or `#4A4A4A` in the dark rail)
- Quiet text links: `color → #FF3B30`
- Primary buttons: `background → #2A2A2A`

### Not designed yet

Flag these before build: **the "Request access" flow itself** (every CTA on the landing page currently routes to sign-in; there is no request form, no confirmation, no invite email), **real form validation and auth error states on sign-in** (the fields are static text, not inputs — no focus ring, no invalid-credentials state, no rate limiting, no password manager affordances), **loading and empty states** (no product yet, analysis in progress, first-run agency), **error states**, **the purchase/checkout flow** (the design stops at "Review order"), **the credits top-up flow**, **Billing**, **the product list**, the **Content** and **History** product tabs, the **brand switcher dropdown**, **search results**, and **responsive behavior** (the design assumes ≥1400px desktop; below the 236 + 1220 + padding minimum, the rail should probably collapse to icons before the content pane reflows).

---

## State Management

The prototype holds three pieces of state. A real implementation should move the first two into the router.

| State | Type | Values | Notes |
|---|---|---|---|
| `screen` | enum | `Dashboard` \| `Product` \| `Analysis` \| `Creative` \| `Shelf` | → route |
| `tab` | enum | `Overview` \| `Competitive` \| `Creative` \| `Content` \| `Share of shelf` \| `History` | → derive from route |
| `clientMode` | boolean | — | user/session preference; persist |
| `signOut` | callback | — | passed into the workspace by the shell; clears session, returns to `/` |

The prototype's public surface holds one more: `screen` = `Site` | `Login` | `App`. That is a stand-in for real authentication — replace it with a session check and a route guard, not a client-side flag.

### Data the screens need

- **Agency**: name, brand count, product count, retailer, KPI aggregates, activity feed
- **Brand**: name, initials, product count
- **Product**: name, brand, item number, price, rating, review count, main image, competitive-set size, asset-profile counts, recommended occasions (with exclusions)
- **Analysis**: composite score, category average, leader score, analysis date, competitor count, six dimension scores each with category average and leader value, ranked recommendations (title, explanation, service, price, CTA target)
- **Creative set**: images grouped into four categories, each with caption, rationale, approval status, revision count; package totals; seasonal versions; projected score
- **Share of shelf**: 8-week series for you / leader / category average, current share, rank, availability, price position, competitive-set table, tracked-term count

---

## Voice & Copy Rules

The copy is part of the design. It follows the Rick Sauls content system, adapted to a product surface:

- **Plain and numeric.** "Lifestyle imagery trails the competitive set by 38 points" — not "Boost your visual appeal!" State the number, name the gap, say what closes it.
- **Sentence case** for all titles and headlines. UPPERCASE only for tracked labels and status pills.
- **Numbers always numeric**: `64%`, `18.4%`, `−38`, `#3`, `8 of 8`. Never spelled out. Never rounded up for effect.
- **No emoji. Anywhere.** For visual texture use a numeric prefix (`01`, `02`) or a 32×2px red rule.
- **No hype vocabulary**: no *revolutionary, unleash, supercharge, AI-powered, next-gen, game-changer, journey, seamless, effortless*. The word "AI" does not appear in the interface at all — the analysis is the product, not the technology behind it.
- **Say what was ruled out.** "Tailgating and Mother's Day were excluded as poor fits." "Recommendation only · no action needed." Showing the negative case is what makes the positive case credible.
- **Bullets are short** — noun phrases or single sentences, 3–5 items.
- `text-wrap: pretty` on all multi-line body copy.

Minus signs are typographic (`−`, U+2212) rather than hyphens: `−17`, `−38`, `−2%`. Middots (`·`) separate metadata fragments throughout.

---

## Assets

- **Fonts** — `fonts/Inter-{Regular,Medium,SemiBold,Bold}.woff2`, included in this bundle. Self-host these; do not swap in a CDN or a system-font stack.
- **Icons** — four inline SVGs total across both surfaces: a chevron (brand switcher), a magnifier (search), and the design system's 1.5px arrow (public-site CTAs, used twice). Nothing else. The design system is deliberately icon-light: **thin 1.5px stroke, square caps, no fills, one icon per card maximum.** If you need more, use [Lucide](https://lucide.dev) — it matches the stroke weight and construction. Resist adding icons to nav items, KPI cards, or table rows; the design reads as serious partly because it doesn't.
- **Logos** — the ecomm-copilot wordmark is set as live text (Inter Bold, −0.02em) rather than an SVG. There is no product logo mark yet. `logo-mark-mono.svg` and the wordmark/lockup files in the Rick Sauls design system belong to the personal brand and should not be used as the product's mark.
- **Product imagery** — none supplied. Every image is a placeholder. Real Walmart product photography is needed, and the interface is built to let it supply all the color.
- **Charts** — no library. One inline SVG (Share of Shelf) and several CSS-only bar/track constructions.

---

## Relationship to the Rick Sauls Design System

This app is built on the Rick Sauls design system, **inverted for a light workspace**: the dark canvas moves to the navigation rail, `#F3F3F3` becomes the page, and `#FFFFFF` becomes the card surface. Everything else is held: Inter only, the same tight display tracking, the tracked-uppercase label, 1px hairlines instead of shadows, square-ish radii (max 4px), sparse layouts, one accent used sparingly, no gradients, no emoji, icon-light.

Two things to know if you extend the design:

1. **The five-color palette has no positive/negative pair.** Red is the only accent and conventionally reads as "bad," so improvements (`+3`, `+6.3`, `+1`) are rendered in neutral `#4A4A4A` rather than a success color. This was a deliberate choice to stay inside the system. If the product later needs true semantic color, that's a design-system amendment, not an ad-hoc addition — don't introduce green in a component.
2. **This is the personal brand of an independent advisor applied to a product.** If ecomm-copilot needs its own identity later (a distinct accent, a semantic color pair, its own mark), plan for the palette to be tokenized from the start rather than hardcoded — which is worth doing regardless.

---

## Files

| File | What it is |
|---|---|
| `01-Landing-and-Login.dc.html` | The public surface: marketing landing page + sign-in. Signing in mounts the workspace, so this file is the full end-to-end walkthrough. **Start here.** |
| `02-Workspace.dc.html` | The authenticated workspace. All five app screens, the rail, the topbar, and Client View. |
| `support.js` | Runtime required for the above to render. Not part of the product. |
| `fonts/*.woff2` | Inter, four weights. **Do** copy these into the real project. |
| `README.md` | This document. |

Both `.dc.html` files are **reference only — do not build on that format.**

To view: open `01-Landing-and-Login.dc.html` in a browser. Scroll the landing page, click "Sign in to the workspace", then "Sign in" — the workspace mounts in place. Navigate it with the rail and in-page links; toggle "Client view" in the top right; sign out from the user block at the bottom of the rail.

The exact hex, px, and percentage values in this README were read off the source; where the two disagree, the source file is authoritative.
