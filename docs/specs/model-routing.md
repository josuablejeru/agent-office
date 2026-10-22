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
> Not checked:
> - Model Garden with real Google credentials (never used in tests). It fails quietly and the catalog still works.
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
  - One routing decision per task, made before it starts, from your last few messages only. Web pages, files and tool results are never shown to the router, so they can't steer it.
  - Any doubt means the agent's own model.
  - A model chosen by hand always wins.
- **Escalation:**
  - The routed model gets an `ask_for_stronger_model` tool. It's a harmless operation: never an approval, never blocked.
  - Escalation also happens automatically after N failed actions in a row. Refused or blocked actions don't count.
  - At most once per task. The stronger model sees what happened so far as text, which works across providers (Claude thinking blocks and Gemini thought signatures stay with the model that made them).
  - Answers show "Auto › model", with ↑ when escalated and the reason on hover.
- The agent's fallback now also receives history as text, for the same reason.

## What is out (on purpose)
- **Channels** keep each agent's own model: no routing there yet.
- **Vision:** if Auto picks a model that cannot read images for an agent that can see, that call
  fails. Route such agents' tasks to models that can see.
- **A routing decision per step** (instead of per task): it would defeat provider-side caching
  and make answers harder to follow.
- **Learned routers and cost dashboards.**
