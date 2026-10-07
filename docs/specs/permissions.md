# Per-agent permissions

> **Status (6 Oct):** built and checked.
> - Unit tests: every level against every kind of rule verdict; Jev cannot loosen; a change
>   mid-task applies to the next action; old agents keep what they had.
> - On real agent computers (`tests/e2e`): "Ask me first" stopped a harmless `echo` for approval;
>   a switched-off browser was neither offered to the model nor run when called anyway; with
>   Memory off nothing was recalled; with Channels off a mention did not start the agent and the
>   channel said why; a second agent was unaffected.
> - In the app's own window type (WKWebView) with a real agent computer: opened Permissions, set
>   "Run commands" to "Ask me first", saved, sent a message, the approval dialog appeared with
>   the reason, "Allow once" ran the command, the answer appeared, and the setting was still
>   there on reopening.
> - A copy of the real database migrated with the existing agent unchanged.
>
> Not checked: the installed app bundle itself was only launched and cycled, not clicked
> through (it runs the same UI files as the window test); the tab with the Jev switch on, since
> no Jev service exists here.

**For:** you, giving different agents different amounts of trust: a research agent that may
browse but not run commands, a new agent that asks before everything.
**Outcome:** on an agent's Permissions tab you choose, per kind of tool, Allowed / Ask me first /
Not allowed, and the agent's very next action follows it.
**Appetite:** half a day.

## What is in
- A **Permissions** tab in each agent's settings (and a Permissions button on the agent page).
- Seven kinds of tool: run commands, files, browser, web search, databases, memory, channels.
- Three levels each:
  - **Allowed**: runs by itself, except where a built-in safety rule asks for approval.
  - **Ask me first**: every action of that kind waits for your approval.
  - **Not allowed**: the agent is not offered the tool, and a call to it is refused.
- **Anything the safety rules do not recognise**: per agent, allowed or ask, overriding the
  app-wide `policy.default_action`.
- The list of things that always ask, whatever is chosen.
- Changes apply to the agent's next action, even in the middle of a task.

## How the decisions combine
Your settings can only make an agent more restricted:
1. Not allowed: refused.
2. A built-in rule's verdict stands. "Allowed" never skips the approval for `rm -rf`.
3. Ask me first: anything that would have run by itself becomes an approval.
4. Jev, if enabled, is asked only about actions no rule covers, and cannot loosen 1 to 3.
5. Otherwise the agent's setting for unrecognised actions, or the app default.

## What is out (on purpose)
- Allow and block lists for websites. Checked only when opening a page, they would miss clicks,
  redirects and `curl`, and so would promise more than they enforce. Needs network-level
  filtering, which is its own piece of work.
- "Always allow this" from the approval dialog, and custom rules in free text.
- Limits on what programs on the agent's computer can reach on the network: these settings
  govern the agent's tools only.

## Known limits
- A tool switched back on in the middle of a task is offered to the model from the next task.
- With "Run commands" allowed, an agent can do in the shell most of what other tools do (read
  files, fetch pages). To really confine an agent, restrict "Run commands" first.
