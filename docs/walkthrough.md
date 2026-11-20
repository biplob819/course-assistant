# Walkthrough GIF: the screens it shows

`docs/assets/walkthrough.gif` is the project's walkthrough in place of a video (D-75). It is
silent, loops, and holds each screen for 2.5 seconds (25 seconds in all, 960 px wide). Screens 1
to 7 are real captures with no key and no spend: the app ran locally on throwaway passcodes and
placeholder secrets, and every answer shown is a stored recording, so no model was called.
Screens 8 to 10 render real terminal output, committed numbers and the architecture diagram.

| # | Screen | Source |
|---|---|---|
| 1 | Learner page: the passcode gate | `deploy/streamlit_app.py`, run locally |
| 2 | Learner page: the privacy notice and consent purposes | same, after unlocking |
| 3 | Answer from the course materials, with its source chip; the pasted injection in the question is ignored | same, worked example 9 |
| 4 | Graded work: a hint and the session to revisit, never the work itself | same, worked example 16 |
| 5 | Not covered: a fixed message, no guess | same, worked example 10 |
| 6 | An admin question sent to the teaching team | same, worked example 23 |
| 7 | Teaching team: pause switches per session | same, Teaching team page |
| 8 | CLI: `init`, `doctor` in a new folder, `eval-replay` 40/40, `make eval-offline` | terminal output, rendered |
| 9 | Release gate: the held-out verdict on heldout-4 (exit 1: grounded and latency miss; every safety gate passes), beside heldout-3 and the public half | `docs/measurements/e2.5-heldout4-report.md`, `docs/measurements/e2.5-heldout4-gates.json`, `docs/measurements/e2.5-heldout3-report.md`, D-87 (retaken 20 Nov 2026) |
| 10 | Technical architecture | `docs/assets/architecture.svg` (retaken 20 Nov 2026) |

The worked examples come from the committed replay fixture
(`src/course_assistant/answer/replay/golden.json`, 40 recorded sample-course requests) and not
from the hosted demo's cache, which needs a paid run by the owner. That is why the source chip
reads "unit text is not kept in the replay fixture". Screens 8 and 9 restate real output and
committed numbers without changing them.

Captured at 1280×800 with headless Chrome (the DevTools protocol for the app interactions).
Screens 9 and 10 were retaken on 20 Nov 2026 (D-90): screen 9 is the heldout-4 verdict rendered
from the committed numbers, screen 10 the current architecture SVG, both rendered with headless
Chrome and scaled to 960 px; screens 1 to 8 are unchanged. The ten screens are joined at 2.5 s
each with a 128-colour palette (Pillow, from a `seq01.png` … `seq10.png` folder):

```python
from PIL import Image
frames = [Image.open(f"seq{k:02d}.png").convert("RGB").quantize(colors=128) for k in range(1, 11)]
frames[0].save("docs/assets/walkthrough.gif", save_all=True, append_images=frames[1:],
               duration=2500, loop=0, optimize=True)
```

The worked-example screens use the replay fixture, so their source chips show no slide text. The
live demo is up (OWNER D-93) and its own cache holds real slide text; retaking screens 3 to 6
from it is optional (D-46).
