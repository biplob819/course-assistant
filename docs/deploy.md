# Deploying the demo (owner guide)

This guide puts the demo on Streamlit Community Cloud in the `portfolio-demos` workspace. The
code side is built and tested offline (node D3.2). These steps need you: your OpenAI account,
your Streamlit account, the repo connection and the secrets. Nothing here goes into the repo.
Never paste a key, a passcode or a hash into an issue, a commit or a chat.

When you finish, record one line in `docs/decisions.md` (step 10). That line is the proof for
catalog row DEM-04, and D3.2 cannot turn green without it.

## 1. Create the demo OpenAI project with a hard limit

1. In the OpenAI dashboard, create a separate project called `course-assistant-demo`.
2. Under the project's limits, set a monthly budget as a **hard limit** (suggested $20), plus
   an email alert at 80%. Spending stops at the limit, whatever the app does.
3. Create an API key for that project only. Keep it ready for step 6 as `OPENAI_API_KEY`.
   Never use the main project's key for the demo.
4. The app's own guard caps each request at $0.50 and each day at $5.00. Both figures must
   stay under the project's hard limit.

## 2. Make the two passcodes and their hashes

The demo has two numeric passcodes of 6 to 12 digits, which only you know (OWNER D-48):

- the **learner** passcode, which opens the learner page;
- the **teaching-team** passcode, which opens the staff page. It must be a different code.

For each one, run this on your own machine:

```
uv run course-assistant passkey-hash
```

It asks for the code twice, without showing it, and prints one line beginning
`pbkdf2_sha256$`. That line is the hash. Use the learner hash as `DEMO_PASSKEY_HASH` and the
teaching-team hash as `STAFF_PASSKEY_HASH`. The app stores only these salted hashes, never
the codes. If the two hashes are equal, the app will not start.

## 3. Create the hosted KV store

Create a free Redis-over-REST database (for example Upstash) in the region closest to the
app. Copy its REST URL as `DEMO_KV_REST_URL` and its REST token as `DEMO_KV_REST_TOKEN`. The
guard keeps the daily spend counter there, so the cap holds across restarts.

## 4. Make `PID_SECRET`

Make a long random value, for example with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`, and keep it as `PID_SECRET`.

## 5. Build and commit the worked examples

With the demo project's key in your local `.env`, build the replay file (the D3.1 step that
is still open). Run `make demo-cache YES=--yes` with your config. It prints the estimated
cost first. Then check it offline with
`uv run python scripts/demo_cache.py --check`, and commit `demo/replay.json`. The file holds
only sample-course text, which is public under CC BY 4.0. The learner page opens on these
worked examples, with no live call. Until the file exists, the page offers live questions only.

## 6. Create the app on Community Cloud

1. At share.streamlit.io, open the `portfolio-demos` workspace and choose **Create app**,
   then deploy from GitHub.
2. Repository: this repo. Branch: `main`. Main file path: `deploy/streamlit_app.py`.
3. Under **Advanced settings**, choose Python 3.12. Community Cloud installs
   `deploy/requirements.txt`, which sits next to the main file.
4. In the same **Secrets** box, paste the names from `.streamlit/secrets.toml.example`, each
   with its value:
   - `OPENAI_API_KEY`, `DEMO_PASSKEY_HASH`, `STAFF_PASSKEY_HASH`
   - `DEMO_KV_REST_URL`, `DEMO_KV_REST_TOKEN`, `PID_SECRET`
   - optionally `DEMO_TRUSTED_PROXY_HOPS` (see the next point)
5. Leave `DEMO_TRUSTED_PROXY_HOPS` unset (or 0) unless you know how many proxies the host
   puts in front of the app. Each proxy must append the client's address to
   `X-Forwarded-For`. With 0, failed passcodes are counted for each browser session, plus a
   global cap for all visitors. That is safe, but 50 wrong guesses in 15 minutes from anyone
   pause every new unlock for 15 minutes. Set 1 only if the host's own proxy appends the real
   client address. A wrong number lets a guesser pose as many addresses.
6. Choose **Deploy**. If a secret is missing, a hash is malformed or the two hashes are equal,
   the app shows only a fixed "not available" page. The log names the missing secret, never
   its value.

## 7. Run the post-deploy smoke check

Open the app once in a browser to wake it up. Then run:

```
uv run --extra app python scripts/smoke_demo.py https://<app-name>.streamlit.app
```

It passes (exit 0) only if the page shows the Passkey input and nothing behind it. A fail
(exit 1) prints the reason. Exit 2 means it could not connect. Then check by hand in a private
browser window:

- The page asks for the passkey, and a wrong code says "Wrong passkey."
- The learner code opens the notice, then the worked examples. A live question answers.
- The **Teaching team** page refuses the learner code and opens with the teaching-team code.

## 8. Show the off switch working

1. On the **Teaching team** page, choose **Pause switches**, tick
   "Yes, pause all sessions" and press **Pause all sessions**.
2. On the learner page, ask a live question. The answer must be the fixed paused message, and
   no model call is made.
3. Press **Resume all sessions**, and check that answers come back.

The hard off switch is to delete `DEMO_PASSKEY_HASH` from the app's secrets. Every page then
closes at its next check. To stop the app entirely, use **Reboot** or **Delete** in the app
menu.

Demo data resets each night: the app keeps one store per UTC day and removes the earlier ones.

## 9. Revoke a passcode

1. Make a new hash with `course-assistant passkey-hash`, using a new code.
2. In the app's settings, under **Secrets**, replace `DEMO_PASSKEY_HASH` (or
   `STAFF_PASSKEY_HASH`) and save.
3. Every session that was unlocked with the old code is locked at its next check, with no
   restart. The old code no longer opens anything.

To close the demo instead, remove the hash.

## 10. Record the attest

When steps 6 to 9 pass, add one line to `docs/decisions.md` (use the next free number):

```
| OWNER D-n | <date> | Attests D3.2: the demo is live in portfolio-demos at <URL>; smoke_demo passed; pause and passcode revocation shown working on the live app | — | Owner deploy |
```

Then set the DEM-04 proof to `attest=OWNER D-n`. The node goes green only after the verifier
has also confirmed DEM-01 to DEM-03.
