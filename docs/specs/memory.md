# Memory and databases for agents

> **Status (5 Oct):** built. Checked live on a separate test agent: remember, recall by words and
> by "about" (two steps), forget; a fact told once was used after clearing the conversation and
> again after restarting the computer; an agent created a table, inserted three rows and counted
> them correctly; a runaway query was stopped after 10 s with the daemon answering straight
> after; `DROP TABLE` raised the approval dialog; the Memory panel listed, searched, added and
> removed entries in the real app window.
>
> Observed and not solved: the test model saved an unverified address it found on the web as a
> fact about a contact. Agents are now told not to do that, but nothing enforces it; the Memory
> panel is where you correct it.
>
> Not checked: memory with many thousands of entries, and two agents' memory side by side.

**For:** you, working with the same agents over days, who should not have to repeat what you
already told them; and agents that need somewhere to keep structured information.
**Outcome:** tell an agent something once ("my accountant is Dana, dana@example.com"), clear the
conversation, ask a question that needs it days later, and get an answer that uses it. An agent
asked to "keep a list of leads" keeps it in a table and can query it back.
**Appetite:** one working session.
**Assumptions:**
- A few hundred to a few thousand remembered facts per agent, not millions.
- Models differ, so memory must help even when the model never thinks of searching it.

## What is in
- **Memory.** Each agent has a private memory on its own computer. A memory entry is about a
  subject and holds a note, an optional link to another thing ("Dana - works at - Acme"), or
  both. Links make it a small graph: asking about one thing also returns what it is connected to.
- **Recalled automatically.** At the start of every task, the entries most relevant to the
  message are put in front of the model, so it does not have to remember to look.
- **Databases.** An agent can create SQLite databases under `~/Databases` and run SQL on them:
  its own tables for leads, expenses, research. The same files are usable from its scripts.
- **Memory panel** in the app: see and search what an agent remembers, add an entry yourself,
  delete entries, and see its databases with their tables and row counts.
- **Safety.** Dropping a table, or deleting or updating every row, needs your approval.

## What is out (on purpose)
- A separate graph database engine. A links table in SQLite with a two-hop lookup covers
  "what do I know about X" at this size, with nothing extra to install or keep running. It comes
  back if agents need many-hop queries over tens of thousands of entities.
- Memory shared between agents. Channels are the shared place for now.
- Embeddings / semantic search. Word search is enough to start and works offline with any model.
- Automatic summarising of old conversations into memory.
- Editing entries in the panel (delete and re-add instead), and a table browser for databases.

## Where it lives, and what that means
On the agent's own computer, in its disk. So: it survives restarts and clearing the
conversation; it is deleted when the agent is deleted; and the agent's computer must be on to
view it. The entries recalled for a task are part of the request to the model provider.

## Riskiest assumption
Injecting recalled entries is enough for an agent to use them without being told to. Cheapest
test: store one fact, clear the conversation, and ask an indirect question.

## Milestones
1. **Memory and SQL on the agent's computer.**
   Check: on a test agent, remember three entries, recall by word and by "about" (two hops),
   forget one; create a table, insert, select; a runaway query is aborted and the daemon still
   answers immediately afterwards.
2. **Agents use it.** Tools, automatic recall, approval rules.
   Check: tell the agent a fact, clear the conversation, ask indirectly: the answer uses it, and
   again after restarting its computer. Ask it to keep a table and query it back. A `DROP TABLE`
   raises the approval dialog.
3. **Memory panel.**
   Check: in the real app window, the fact is listed, a search narrows the list, deleting removes it.

## Decisions for you
- **Recalled memory goes to the model provider.** With a local model nothing leaves the Mac; with
  a hosted one, what an agent remembers is sent with its requests. My recommendation: accept it,
  since it is the same data that was in the conversation, and avoid storing secrets in memory.

## First step
Write the memory and SQL operations and check them on the test agent.
