# Model switching

> **Status (21 Oct):** built. Checked:
> - Unit tests:
>   - a switched task runs on the chosen model, and the next one on the agent's own;
>   - the agent's fallback still stands behind a switched model;
>   - an unknown provider or half a choice is refused before anything starts;
>   - the model list is validated (known provider, unique names, at most 20) and a refused save changes nothing.
> - In the app's window type, with a real agent computer and a stand-in model server:
>   - a model added under Settings › Model providers;
>   - the chat's switch offering it;
>   - a task sent to it, with its answer labelled;
>   - the choice kept across a reload;
>   - switching back sent the next task to the agent's own model.
>
>   The model server saw one request for each model.
>
> Not checked: real Claude, Gemini or other hosted models through the switch (same provider code as an agent's own model).

**For:** you, using quick, cheap models for simple jobs and strong ones for hard jobs, without
editing an agent each time.
**Outcome:** one click next to the message box sends the next task to another model.

## What is in
- **Settings › Model providers › Models to switch between:** short names for models you use
  (*Haiku*, *Opus*, *Gemini Flash*), each with a provider, a model ID (suggested from the
  provider where it can list them) and Test. Stored in `config.yaml` under `models`.
- **Next to the message box:** the agent's own model (default) and the list. The choice applies to
  the tasks you send to that agent and is remembered per agent in this window; a name removed in
  Settings falls back to the default.
- **Each answer** shows which model wrote it, highlighted when it was not the agent's own.
- The agent's fallback model stays behind a switched model.

## What is out (on purpose)
- **Channels** keep each agent's own model. A channel has no switch.
- **The agent choosing its model itself** (routing by difficulty) comes later, once it's clear which
  jobs go to which model.
