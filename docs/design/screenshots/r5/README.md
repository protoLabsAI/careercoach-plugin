# R5 narrow-width screenshots (§8)

Real renders of the shipped Jobs view (`_DASHBOARD_HTML` in `__init__.py`) — **not** mockups — at
the three §8 container bands in light and dark, seeded at the spec volumes (49 inbox, 2
saved/preparing, 15 applied with 3 follow-ups flagged, 1 interviewing, 86 closed = 83 rejected + 3
passed; fit shown on 4 roles). The default Pipeline tab is shown because the headline §8 change is
the board → sideways-scroll → stage-list transition.

| File | Container width | Band (§8) | Theme |
|---|---|---|---|
| `r5-900-light.png` / `r5-900-dark.png` | 960px | ≥900: full board, 5 shared columns + 120px Closed stub | light / dark |
| `r5-720-light.png` / `r5-720-dark.png` | 720px | 600–899: board scrolls sideways, 220px snap columns | light / dark |
| `r5-420-light.png` / `r5-420-dark.png` | 420px | <600 rail: stage-grouped Accordion list, Today capped at 2 rows + "+N more", target-roles subline hidden | light / dark |

How they were made: the view was served with the live `/_ds/plugin-kit.css` (the real `--pl-*`
tokens, so contrast is accurate in both themes), its `/jobs` + `/state` routes seeded at the volumes
above, and captured with headless Chrome at a pinned `.cc` container width (the layout reads the view
root's inline-size, so the band depends on the container, not the OS window).
