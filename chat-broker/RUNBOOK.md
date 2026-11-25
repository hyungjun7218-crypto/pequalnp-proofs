# Chat-only contribution runbook

This fork has two isolated branches. Never merge these branches into main or send them upstream. No Work, Codex, paid model API or local PC is needed for the supported steps.

## Broker
- On chat-lean-broker, update chat-broker/request.json with a fresh session string (8–80 ASCII letters/digits/underscore/hyphen) and mode normal, hard or selftest. Do not launch another session while one is live.
- Read chat-broker/state.json and verify the session matches. A no_gap response means stop; do not poll the desk aggressively. A selftest does not claim a gap.
- The lease remains in the runner's memory only. The broker renews every 8 minutes (inside the official 10-minute window), stops on 410, and attempts release after 25 minutes without a new control request or 90 minutes total. It does not run any model. Read state again before each check and submission. If a session expires, do not continue on an invalid lease.
- To control the session, create or update chat-broker/control.json with {"session":"...","gap":"L-...","seq":1,"op":"check_status"}. Use a strictly increasing sequence per session. Operations: check_status, renew, prepare_submit (include commit), release/stop (include reason). For submitted/reduced include actual PR number as pr; for reduced also file; for exhausted include program SHA256. Wait for matching last_seq and successful state; don't leak lease IDs.
- Commit status context chat-lean-broker links to the workflow run. State updates by the bot don't trigger new runs.
- A successful selftest establishes state publication, run lookup and synthetic response classification only, not live Desk access, renewal or release.
- Live requests log DESK REQUEST GET (public handle only) and DESK RESPONSE (HTTP status, whether the JSON is an object, whether the gap key is present, classification, and the bounded no-gap note). Never log a full response, lease ID or secret-bearing URL.
- A no_gap result requires HTTP 200, a JSON object with an explicit null gap and no lease or error. HTTP errors, malformed JSON, missing/invalid gap fields, server errors and missing lease IDs have distinct error statuses. Do not treat them as a lack of work.
- The selftest exercises synthetic normal, malformed, server error, transport error and secret-redaction cases without contacting or claiming a gap from the Desk.

## Preflight
- Read official join.txt, CONTRIBUTING.md and current upstream configuration. Acquire a valid assigned gap before writing the proof.
- Obtain a recent upstream main commit SHA and inspect its exact definitions. On chat-lean-check create/update chat-check/candidate.json containing {"gap":"L-...","file":"lean/PEqualNP/Outside/L_....lean","kind":"check","base":"40 hex upstream SHA","proof":"exact full UTF-8 Lean source"}.
- The workflow fetches candidate as data, checks the upstream ancestor, retrieves the official desk statement, validates format/imports and runs the adapted locked authoritative checker. Follow commit status context chat-lean-check to obtain run and job logs.
- Only treat VERDICT: ACCEPTED with successful job and matching candidate/source/statement hashes as successful preflight; this is NOT the upstream official verdict. Fault is infrastructure, rejection is a proof/format issue. Repair and update the candidate if the lease is still held.
- Do not include the broker or checker files in any proof PR.

## Submit
- Ask broker prepare_submit with candidate commit and confirm matching sequence and ready_to_submit, valid state and expiry.
- Create a fresh proof branch in this fork directly from the verified upstream base SHA. Add exactly the validated file, with identical bytes. Compare with upstream and verify exactly one added path, no other changes; don't submit if this cannot be checked.
- Open a PR against open-goat/pequalnp-proofs main using the user's connected GitHub identity. Verify PR number, author and file list, then issue release submitted (or reduced) to the broker and verify response.
- Read authoritative PR verdict and merge status when available. Report pending honestly. Chat doesn't keep reasoning or monitoring after its response ends.
- If unable to finish, release with accurate reason. Do not submit unverified work or open test PRs to upstream.
