---
icon: lucide/sparkles
---

# AI copilot

The copilot is an interactive assistant that helps you write notebook code. It
chats with you in a second browser tab beside your notebook and can **propose
cells** that you apply with one click. It is **schema-only**: it sees your
columns' names and types and your notebook's code, but never the data itself —
see [why the copilot can't see your data](../admins/ai-privacy.md).

!!! note "Running a frozen build?"
    The CLI examples below use the bare `mooring` command. Running a frozen
    `.pyz`/`.exe` build instead? Use `python mooring.pyz <cmd>` (or
    `mooring.exe <cmd>`).

## One-time setup

The copilot ships as an optional extra (it brings GitHub's Copilot CLI, which is
fetched automatically on first use, so the first sign-in needs internet access).
On the simple path — Python 3.12 or newer with uv/PyPI — there's no git to learn
and no personal access token to juggle; you just install and sign in. Install
mooring with the `copilot` extra — pick the form that matches how you run it, and
quote the brackets so the shell doesn't glob them:

```
uv tool install "mooring[copilot]"   # install as a persistent CLI (recommended)
uv add "mooring[copilot]"            # …or add it to your own uv project
pip install "mooring[copilot]"       # …or with plain pip
uvx "mooring[copilot]"               # …or a one-off run (doesn't stay installed)
```

Then sign in to GitHub Copilot. You can do it **from the hub** or from the
command line — either works:

- **In the hub** — open the **🤖 Copilot** menu in the header to see whether
  Copilot is connected (and as which account). Click **Sign in to Copilot** to
  authorise in a browser; **Switch account** changes which account is used. If you
  open the chat before signing in, it shows a **Sign in to Copilot** button right
  there too.
- **From the command line** — these assume `mooring` is on your `PATH` (installed
  via one of the first three forms, not a one-off `uvx` run):

    ```
    mooring ai login                # sign in to GitHub Copilot (opens a browser)
    mooring ai status               # check you're connected (shows the account)
    ```

!!! info "Copilot is a separate sign-in from your GitHub login"
    The **Log in with GitHub** button connects mooring to your team's notebook
    **repo** (for sync). GitHub **Copilot** signs in separately and can even be a
    **different account** — so signing into GitHub doesn't sign you into Copilot,
    and vice versa. The hub's sign-in card shows which Copilot account is connected
    so you can tell them apart.

The `pii` and `pii-spacy` extras add optional name detection on top of the PII
guard — installing one only makes it *available*; you still switch it on in config
(see [Turn on the PII guard](#turn-on-the-pii-guard)). For the full extras
reference, see
[optional extras](../admins/build-and-distribute.md#optional-extras). None of it is
required to get started.

You need a GitHub Copilot licence, and your organisation must have the Copilot
CLI/agent policy enabled. If the extra isn't installed, the chat will tell you.

## Using it

1. In the hub, open a notebook (**Open**) and, on the same row, click **AI** —
   the chat opens in a new tab.
2. Optionally pick a **dataset** (so the assistant knows your columns and types),
   a **model**, and a reasoning **effort**.
3. Ask for what you want — e.g. *"filter to 2024 and total `amount` by `region`"*.
   While it works you'll see a thinking indicator and a status line
   (*"Looking up the schema…"*); the reply then streams in with formatted code.
4. Click **Apply ▸** on a proposed cell: it's written into your notebook and runs
   there. Review it like any other cell.

Keep both tabs side by side: chat on one, the marimo notebook on the other.

Picking up a teammate's notebook? Pull it, open any synced notebook, and ask the
copilot about its schema straight away — see the
[5-minute quickstart](quickstart.md).

!!! warning "Never paste real values"
    Anything you type into a cell or the chat is visible to the assistant. Refer
    to columns by name — don't paste actual data values.

    If your admin has turned on the **structured-PII scan**, a message that looks
    like it contains a payment card, IBAN, NHS number, email, or UK NINO is **held**
    and you'll see a **"Send anyway"** prompt — nothing is sent until you confirm.
    It's a best-effort safety net: it won't catch names, sort codes, or account
    numbers, so the rule still stands. See
    [the privacy page](../admins/ai-privacy.md#structured-pii-pre-flight-scan-opt-in-best-effort).

## Turn on the PII guard

The copilot is schema-only by design, so your **data** never reaches the model. On
top of that, mooring ships an **opt-in, best-effort** scan of the text *you* type —
it is **off by default**, and installing an extra doesn't enable it. Switch it on
when you'll be working near sensitive identifiers:

```bash
mooring config set ai.pii.enabled true       # structured scan: cards, IBANs, NHS numbers, emails, UK NINOs
mooring config set ai.pii.detect_names true  # also flag person/org names (needs the pii or pii-spacy extra)
mooring ai pii doctor                         # show which name backend will run, and whether it's ready
```

- **Structured scan** (`ai.pii.enabled`) is pure-offline and needs no extra. On a
  hit it **holds** the prompt with a *"Send anyway"* confirm — set
  `ai.pii.block_prompt false` for a warn-only advisory instead.
- **Name detection** (`ai.pii.detect_names`) needs a local NER model: either the
  `pii` extra (GLiNER — downloads a pinned model from Hugging Face on first use;
  pre-fetch with `mooring ai pii model`) or the fully offline `pii-spacy` extra for
  air-gapped teams (model bundled, no download). With either installed,
  `ai.pii.name_backend` can stay `auto` and the right backend is chosen for you.
- Scan your files *before* enabling with `mooring ai pii check`.

It's defence in depth, **not** a guarantee — it can't catch every identifier (no
sort codes, account numbers, addresses, or values typed into prose), and the
schema-only design remains the real protection. Your admin can also bake these
settings into the build so they're on for the whole team. See
[the privacy page](../admins/ai-privacy.md#structured-pii-pre-flight-scan-opt-in-best-effort).

## Turn AI off for a notebook

Sometimes you want the copilot's help setting a notebook up, but later need to
work with **PII** in it (real values in filters, say) and want to be sure AI can't
be opened on it by mistake. You can turn the copilot **off for that one notebook**
from either place:

- **In the hub** — on the notebook's row, click **Disable AI**. The **AI** button
  disappears; the row now shows **Enable AI** to turn it back on.
- **In the chat window** — click **Disable AI** in the top bar. The chat locks
  immediately and offers an **Enable AI** button if you change your mind.

Once a notebook is off, opening the chat for it is refused, and any chat window
already open for it stops working — so a stale tab can't slip a message through.

!!! info "It's shared with your team"
    The decision is saved to a `mooring.toml` file at the top of your workspace and
    **travels with the notebook**: once you **push** it, everyone who syncs the repo
    gets the copilot turned off for that notebook too. `mooring.toml` shows up as a
    normal file to push/pull, and it stores only notebook **paths** — never any data.
    (If two people edit it at once it resolves like any other file conflict.)

## Team context (optional)

If your admin enables it (`[ai] context = true`), the copilot also reads a
`context/` folder in your workspace so it understands *your* data, not just the
columns of the file you opened:

- **`context/instructions.md`** — house rules in plain English ("report amounts in
  GBP millions", "exclude test accounts"). Sent to the assistant on every turn.
- **`context/dictionaries/*.yaml`** — your team's data dictionary (dbt
  `schema.yml` works out of the box; one file per domain). The assistant pulls in
  the tables relevant to what you're working on and can look up others on demand —
  so it can write correct joins and SQL using your real table and column names.

Only metadata crosses the wire — table/column **names, types, keys, and
descriptions**, never sample values. Run `mooring ai dictionary check` to see how
your files parse and to catch anything sensitive *before* you share them.

!!! warning "These files are sent verbatim and shared"
    Unlike the dataset schema, `context/` files contain whatever you write and are
    shared with your team. Never put real data values or secrets in them — see
    [the privacy page](../admins/ai-privacy.md#team-context-opt-in-not-a-structural-guarantee).

## Tips

- Keep the notebook tab open beside the chat so applied cells appear live; if
  it's closed, the cell is still saved and shows next time you open the notebook.
- **Model & effort:** pick a model from the dropdown; for models that support it,
  a higher reasoning **effort** trades speed for more thorough answers. Your
  choice is remembered. (Some models — and "Auto" — have no effort setting.)
- The assistant can read your notebook's current code and ask for a dataset's
  schema on its own; you don't need to paste either in.
- It also sees the schema of dataframes already loaded in the running notebook —
  and this is **refreshed every time you send a message**, so if you load a new
  dataframe mid-chat, just ask your next question; there's no need to reopen the
  chat. (It sees column names and types only, never your data values.)
- It writes Polars (`pl`) by default, matching mooring's bundled notebook stack.
