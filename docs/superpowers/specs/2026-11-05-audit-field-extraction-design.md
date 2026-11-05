# Audit field extraction (step 1 of countable log analytics)

Date: 2026-11-05 · Status: draft for review · Branch: feat/ai-action-approval-workflow

## Decision (confirmed 2026-11-05)

Hidden users (section 4): **B, stable pseudonyms for e-mail addresses**, because the questions "who is the most frequent
user" and "user login report" need to tell people apart. (Option A, keeping `[REDACTED:email]` and letting the agent say
"unknown users", was not chosen.)

## Goal

Make questions like "how many times did a user log in", "which application is used most", "which application has the
most failures", "user login report per day" answerable by exact counting instead of by the agent sampling 25 lines.
Step 1 only stores the facts as fields. The tool that counts them is step 2 and gets its own spec.

## What the data looks like (374 audit lines in `sagar-wso2/push`, measured 2026-11-05)

- Two shapes of the same `AUDIT_LOG` message: pipe style `Initiator : x | Action : y | Target : z | Data : { ... } |
  Result : r` (173 lines) and equals style `Initiator=x Action=y Target=z Data={...} Outcome=r` (201 lines).
- 15 actions (`Get-User-Claim-Values` 155, `Login` 60, `UpdateSession` 44, ...). Results seen: `Success` 372, `Failed` 2.
  A failed login is an INFO line, so a level filter never finds failures.
- `ServiceProviderName` (the application) is a key inside the Data JSON; some actions (e.g. `UpdateSession`) have none.
- The initiator is `[REDACTED:email]` on 179 of 374 lines, and the logged-in user on 48 of 60 logins. The same person also
  appears as `Goutam001`, `PRIMARY/Goutam001` or a UUID.

## Design

### 1. Where it runs

In the WSO2 text layout (`app/ingestion/fields/text_layouts.py`, `resolve_text_event`): when the layout is
`wso2_carbon` and the logger is `AUDIT_LOG`, call a new pure function `audit_fields(message) -> dict[str, str]` in a new
module `app/ingestion/fields/audit.py` and merge its result into the `fields` dict the layout already returns. That dict
is what `prepare_event` stores in `log_events.fields` (JSONB), for file-indexed and pushed events alike.
No table or column changes. Signatures, semantic text and embeddings are not affected by this part.

### 2. Fields

Keys are prefixed `audit_` so they cannot collide with the layout's `tenant`, `app`, `pid`, `thread`.

| Key | Source | Rule |
|---|---|---|
| `audit_action` | `Action` | token of letters and `-` |
| `audit_result` | `Result` (pipe) or `Outcome` (equals) | as logged (`Success`, `Failed`); consumers compare case-insensitively |
| `audit_actor` | `Initiator` | a leading `<STOREDOMAIN>/` (e.g. `PRIMARY/`) is removed |
| `audit_target` | `Target` | as logged |
| `audit_user` | `"AuthenticatedUser"` in Data | same domain-prefix rule; present on logins only |
| `audit_application` | `"ServiceProviderName"` in Data | present only when the line has it |

A key is omitted when its value is missing or empty. Nothing else from Data is stored; in particular not `Claims`
(roles, groups, user ids), which is personal data and large.

### 3. Parsing

Per-key bounded regular expressions on the redacted message, not a JSON parse: the two shapes then need no separate
code, nested `Claims` objects cannot confuse it, and a half-formed line still yields what it has.
Each value is limited to 256 characters and the scan to the first 16 KiB of the message (the layout already caps
text). The first match of a key wins. A line that matches nothing gives an empty dict, and the event is stored as today.
The function never raises (the resolver's existing guard still wraps it).

### 4. Hidden users: stable pseudonyms (option B)

Today the e-mail rule in `app/ingestion/redaction.py` replaces every address with `[REDACTED:email]`, so all hidden people
look like one user. With B the mask carries a code: `[REDACTED:email:u1a2b3c4d5e6]`.

- Code = the letter `u` followed by the first 11 hex characters of `HMAC-SHA256(key, lower-cased address)`. The same
  address always gives the same code; the address cannot be read back without the key. The letter matters: the card-number
  rule runs after the e-mail rule and looks at runs of digits, so a code that happened to be all digits could be
  rewritten as `[REDACTED:card]` and break the mask. A leading letter makes that impossible.
- The key is a new setting `PSEUDONYM_KEY` (in `.env`). **Without a key the mask stays `[REDACTED:email]`** (today's
  behaviour) and one warning is logged at startup, so a missing key never weakens redaction.
- `_ALREADY_MASKED` and the mask-aware scanner in `redaction.py` must accept the optional `:code`, so redaction stays
  idempotent (the push path redacts twice).
- `app/ingestion/signature.py` must first reduce `[REDACTED:<kind>:<code>]` to `[REDACTED:<kind>]`, so every user of
  one kind of event keeps one signature. Existing rows are unaffected (their masks have no code).
- `audit_actor` and `audit_user` then hold `[REDACTED:email:u1a2b3c4d5e6]` for hidden people and the plain name for the
  rest. The "missing key" warning is logged once, at app start (`lifespan`). Rows stored before this change keep the bare `[REDACTED:email]`: their addresses are already gone and cannot be
  recovered. Step 2 shows those as "unknown (hidden)".

Trade-offs, stated plainly: anyone holding the key can test a guessed address against the codes, so the key is a secret
and is never logged. Changing the key changes every code, so people stop matching their earlier rows. Pseudonyms make one
person linkable across lines, which plain redaction did not. The 12-character code gives negligible collisions below
about a million distinct addresses.

### 5. Existing rows

A new script `scripts/backfill_fields.py`, modelled on `scripts/backfill_signatures.py`, recomputes `fields` from the stored
`raw_text` with the same resolver and writes only rows whose `fields` changed (embeddings, signatures and text are not
touched). Unlike the signature script it iterates `project.sources`, so the pushed events are included. Options:
`--project`. The 374 existing audit rows get action, result, application and non-hidden users; hidden users stay
`[REDACTED:email]`.

### 6. Side effects to know about

- A pushed event's dedup key hashes the redacted text, which now contains the code. An event pushed before this change and
  pushed again after it has a different key and is stored once more. After the change, retries stay deduplicated.
- Semantic text and keyword search see the pseudonym code instead of the bare mask for new rows.

## Testing

pytest, in the existing style:

- `audit_fields`: the real lines of both shapes (login success, login failed, claim lookup with `ServiceProviderName`,
  `UpdateSession` without it, `PRIMARY/` prefix, UUID initiator, hidden e-mail with and without a code); a hostile or huge
  line stays fast and gives a bounded result; a non-audit message gives `{}`.
- Layout: only `wso2_carbon` + `AUDIT_LOG` lines get audit keys; other loggers and other layouts do not.
- Redaction: same address gives the same code, different addresses different codes, case-insensitive; a different key
  gives a different code; no key gives the plain mask and one warning; redacting twice changes nothing; the code never
  contains the address, always starts with `u`, and is never rewritten by the card rule (checked over many addresses).
- Signature: two events differing only in the hidden user have one signature; pre-change signatures are unchanged.
- Integration: a pushed audit line shows the `audit_*` keys in `log_events.fields`; the backfill fills old rows and a
  second run changes nothing.
- The full suite, and the agent eval afterwards (signatures and text are touched by section 4).

## Non-goals

The counting tool and its prompt rules (step 2), expression indexes on `fields` (add when step 2 shows it is slow),
other log types or other audit formats, mapping a user UUID back to a name, and changing the pseudonym of rows that are
already stored.
