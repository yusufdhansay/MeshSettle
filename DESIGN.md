# MeshSettle — Design

There is a minimal demo UI, just enough to visually show a packet's
journey (sender to relay to bridge to settled), not a full product UI.
Keep it clean and boring on purpose, this is not the point of the project.

## Design Philosophy
Clean, minimal, no decoration for its own sake. Think Vercel's own
dashboard and marketing site: generous whitespace, one accent color,
system fonts, no gradients, no shadows beyond subtle depth cues, no
unnecessary animation. Reference: vercel.com's own design language
(monochrome base + one accent, sharp typography, restrained motion).

## Colors
- Background: near-white `#FAFAFA` (light mode), near-black `#0A0A0A`
  (dark mode, if implemented)
- Primary text: `#171717`
- Secondary text / muted: `#737373`
- Borders/dividers: `#E5E5E5`
- Accent (single accent color only, used for primary actions and
  status: settled = green `#16A34A`, pending = amber `#D97706`,
  rejected = red `#DC2626`)
- No secondary brand color beyond the status colors above

## Typography
- System font stack: `-apple-system, BlinkMacSystemFont, "Segoe UI",
  Roboto, sans-serif` or a single clean font like Inter if a webfont
  is wanted
- One weight for body (400), one for headings/emphasis (600)
- No more than 3 font sizes on any single screen

## Components
- Flat cards with a 1px border, no drop shadows, 8px corner radius
- Buttons: solid fill for primary action, outline for secondary,
  no gradients
- Status shown as a small colored dot + label, not a large badge
- A simple horizontal stepper/timeline component to show packet
  progress: Created → Relayed → Bridged → Settled/Rejected
- Monospace font for anything showing a hash, signature, or packet ID

## What to avoid
- No flashy animations, no particle effects, no illustration-heavy
  hero sections
- No more than one accent color outside the status colors
- No dense dashboards, this UI exists to demonstrate the flow clearly,
  not to look like a fintech product
