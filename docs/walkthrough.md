# Walkthrough GIF: the screens it shows

`docs/assets/walkthrough.gif` is the project's walkthrough in place of a video (D-75). It is
silent, loops, and holds each screen for 2.5 seconds (25 seconds in all, 960 px wide). Screens 1
to 7 are real captures with no key and no spend: the app ran locally on throwaway passcodes and
placeholder secrets, opening on the committed demo cache (`demo/replay.json`, D3.1), so every
answer shown is a stored recording with real sample-course slide text and no model was called.
Screens 8 to 10 render real terminal output, committed numbers and the architecture diagram.

| # | Screen | Source |
|---|---|---|
| 1 | Learner page: the passcode gate | `deploy/streamlit_app.py`, run locally |
| 2 | Learner page: the privacy notice and consent purposes | same, after unlocking |
| 3 | Answer from the course materials, with its source chip; the injected "new rule" in the question is ignored | same, worked example 6 |
| 4 | Graded work: a hint and the sessions to revisit, never the work itself | same, worked example 14 |
| 5 | Not covered: a fixed message, no guess | same, worked example 10 |
| 6 | An admin question sent to the teaching team (the D-85 handover text) | same, worked example 18 |
| 7 | Teaching team: pause switches per session | same, Teaching team page |
| 8 | CLI: `init`, `doctor` in a new folder, `eval-replay` 40/40, `make eval-offline` | terminal output, rendered |
| 9 | Release gate: the held-out verdict on heldout-4 (exit 1: grounded and latency miss; every safety gate passes), beside heldout-3 and the public half | `docs/measurements/e2.5-heldout4-report.md`, `docs/measurements/e2.5-heldout4-gates.json`, `docs/measurements/e2.5-heldout3-report.md`, D-87 (retaken 20 Nov 2026) |
| 10 | Technical architecture | `docs/assets/architecture.svg` (retaken 20 Nov 2026) |

Screens 1 to 7 were retaken on 20 Nov 2026 from the app on the committed demo cache; the
earlier capture (18 Nov) used the replay fixture and showed handover wording retired by D-85.
Screen 8 restates real CLI output; screens 9 and 10 were rendered on 20 Nov 2026 from the
heldout-4 numbers (D-90) and the current architecture SVG with headless Chrome. All ten are
1280×800 captures scaled to 960 px and joined at 2.5 s each with a 128-colour palette (Pillow,
from a `seq01.png` … `seq10.png` folder):

```python
from PIL import Image
frames = [Image.open(f"seq{k:02d}.png").convert("RGB").resize((960, 600)).quantize(colors=128)
          for k in range(1, 11)]
frames[0].save("docs/assets/walkthrough.gif", save_all=True, append_images=frames[1:],
               duration=2500, loop=0, optimize=True)
```

The live demo is up (OWNER D-93); retaking from it is optional (D-46).
