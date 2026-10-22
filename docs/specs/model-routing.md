# Model catalog and automatic model choice

> **Status (22 Oct):** built. Checked:
> - Unit tests:
>   - **Catalog:** models that worked come first; a failed listing still returns what is known; Test, saved agents and finished tasks are remembered.
>   - **Model Garden parsing**, against a stand-in: paging, non-chat models left out, billed to the user's project, errors.
>   - **Route numbers** are read strictly. A timeout, an error, an unclear answer, "none fits" and a missing model all lead to the agent's own model.
>   - **The router input** holds only the user's own words.
>   - **A model chosen by hand** wins over Auto.
>   - **Escalation:** it happens after N failed actions in a row, and when the model asks; never on refused or blocked actions; at most once; never shown as an approval.
>   - **Flattened history** has no tool calls, signatures or images, and both message formats accept it.
>   - **Routing settings** must name models from the list.
> - In the app's window type, with a real agent computer and a stand-in model server:
>   - routes set up in Settings, and Auto chosen in the chat;
>   - a coding task went to Opus;
>   - a quick question went to Haiku, three real commands failed on the agent's computer, and Opus finished the task;
>   - each answer was labelled, with the reason on hover.
>
> - With the local `qwen3:8b` (real model, no Google):
>   - the router chose as expected for 4 of 4 requests, taking 8 to 14 s each, so a slow local router model can hit the 20 s limit and fall back;
>   - after three failed actions, the escalated model made real tool calls in 10 of 10 tries. Before the history was told as one report, it imitated the earlier turns as text in 2 of 5.
> - A database from v0.5.0-rc1 opens: new columns are added, and old runs and messages read correctly.
>
> - In the app's window type, with a real agent computer (22 Oct):
>   - the Models & routing section and its overview;
>   - an agent switched to Auto in its settings, then shown as "Auto" in the list and the header;
>   - the chat switch offering "Auto (default)" and the agent's own model;
>   - a chat task routed by default, and one sent to the agent's own model on request;
>   - a channel post answered through Auto;
>   - the on/off switch: off hid the routes, the chat switch and the agent's Auto setting, and a task by an agent set to Auto went to its own model without asking the router. On again brought everything back.
>
> Not checked:
> - Model Garden with real Google credentials. A Vertex provider is listed only once it has a project; listings fail quietly. One check by hand on 22 Oct did reach Google with the developer's credentials (HTTP 403, read-only) before this guard and the test-wide credential block existed.
> - Real Claude/Gemini as router and routed models.
> - Escalating between Claude and Gemini against the real APIs. Handled by turning earlier tool turns into text, which only the stand-in has seen.

**For:** you, with several models on Vertex and elsewhere, wanting quick jobs on cheap models and
hard ones on strong models without choosing each time.
**Outcome:** model IDs are suggested, never retyped; **Auto** picks a model per task and hands over
to a stronger one when the first gets stuck.

## How others do it (October 2026)
- **LiteLLM** Router: tiers chosen by a local heuristic scorer or a small LLM classifier,
  fallbacks, session affinity. It is not used here: its PyPI releases were compromised twice in
  2026, the latest being 1.107.0 on 21 October. A signed app holding Keychain credentials must
  not pull it in, and its router needs LiteLLM's own calling layer, while this app has its own.
- **RouteLLM** (LMSYS): trained strong/weak classifiers. Unmaintained since 2024, two models only.
- **Arch-Router** (Katanemo): routes written as plain-language policies, matched by a 1.5B model.
  The idea is used here, with your own fast model as the matcher.
- **Claude Code**: Opus to plan and Sonnet to execute (`opusplan`), and cheaper models for subagents.
- **Cascades** (FrugalGPT and later work): start cheap and escalate when a check fails. Used here
  as escalation.

## What is in
- **Catalog** (`known_models`): model IDs per provider, from provider listings, Vertex AI Model
  Garden (`v1beta1/publishers/{google,anthropic}/models`), passing Tests, agents' models and
  finished tasks. Suggested in every model field, recently working first.
- **Automatic model choice** (Settings › Model providers): routes in plain words, each to a model
  from the list; the model that chooses; and optionally a model to escalate to after N failed
  actions in a row (default 3).
- **Auto** in the chat's model switch:
  - One routing decision per task, made before it starts, from your last few chat messages only. Channel posts, web pages, files and tool results are never shown to the router, so they can't steer it.
  - A model that routing uses can't be renamed or removed from the list until routing stops using it.
  - Any doubt means the agent's own model.
  - A model chosen by hand always wins.
- **Escalation:**
  - The routed model gets an `ask_for_stronger_model` tool. It's a harmless operation: never an approval, never blocked.
  - Escalation also happens automatically after N failed actions in a row: errors, timeouts and exit codes of 2 or more. Exit code 1 doesn't count (grep found nothing, test said no), and neither do refused or blocked actions.
  - At most once per task. The stronger model is told what happened so far in one report from the user's side, which works across providers (Claude thinking blocks and Gemini thought signatures stay with the model that made them) and which models don't imitate.
  - Answers show "Auto › model", with ↑ when escalated and the reason on hover.
- The agent's fallback now also receives history as text, for the same reason.

- **An on/off switch** (`app.model_switching`, Settings › Models & routing, 22 Oct): off by default. Off means
  every task uses its agent's own model: no chat switch, no Auto, no escalation. The server refuses a model
  choice or Auto while it is off, so nothing reaches past it. If the setting was never chosen, it is on when
  models were already named, so earlier setups keep working after the update.
- **Shown everywhere a model is chosen or used** (22 Oct):
  - Settings › Models & routing (named models, routes, and an overview of which agent uses what);
  - an agent's settings (one-click picks, and "Tasks use: This model / Auto", stored as `auto_model` and as `model_choice` in `agent.yaml`);
  - the agent list and the agent's page ("Auto", or the model's name);
  - the chat switch (the agent's default first);
  - each answer.

  Agents set to Auto also route channel tasks that the user started; the router reads only the user's post.

## What is out (on purpose)
- **Hand-offs between agents** in channels use the agent's own model: their text can quote web
  pages, and the router must only read the user's own words.
- **Vision:** if Auto picks a model that cannot read images for an agent that can see, that call
  fails. Route such agents' tasks to models that can see.
- **A routing decision per step** (instead of per task): it would defeat provider-side caching
  and make answers harder to follow.
- **Learned routers and cost dashboards.**
