# Walkthrough GIF: the screens it shows

`docs/assets/walkthrough.gif` is the project's walkthrough in place of a video (D-75). It is
silent, loops, and holds each screen for 2.5 seconds (25 seconds in all, 960 px wide). Every
screen is a real capture with no key and no spend: the app ran locally on throwaway passcodes
and placeholder secrets, and every answer shown is a stored recording, so no model was called.

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
| 9 | Release gate: the held-out verdict (exit 1, gate fails) | `docs/measurements/e2.5-report.md`, quality table |
| 10 | Technical architecture | `docs/assets/architecture.svg` |

The worked examples come from the committed replay fixture
(`src/course_assistant/answer/replay/golden.json`, 40 recorded sample-course requests) and not
from the hosted demo's cache, which needs a paid run by the owner. That is why the source chip
reads "unit text is not kept in the replay fixture". Screens 8 and 9 restate real output and
committed numbers without changing them.

Captured at 1280×800 with headless Chrome (the DevTools protocol for the app interactions) and
joined with ffmpeg at a 128-colour palette:

```bash
ffmpeg -framerate 1/2.5 -i seq%02d.png \
  -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=5" \
  -loop 0 docs/assets/walkthrough.gif
```

Retake it from the deployed demo once that is live (D-46), so the answers show real slide text.
