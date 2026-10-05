# Next three: make agents finish real tasks

> **Status (5 Oct):** all three are built. Checked live against a separate test agent: the
> original phone-number task (50 s, number traced to its source page), a 20 MB file round trip
> with matching checksums, an agent summarising an uploaded file into `~/Shared`, and Test against
> a real model, a made-up model, a wrong address and a missing key; Files and Test also in the
> real app window.
>
> Not checked: the macOS file-picker dialog itself, the Test button inside the agent form, the
> Bing fallback against the live site, re-finding moved elements on real websites (only on a
> purpose-built page), file permissions of a saved download after the last edit, and any live
> call to Vertex, Anthropic or OpenAI.
>
> Known limits: search reads the engines' result pages and returns nothing if their HTML
> changes; uploading a name that already exists in `~/Shared` replaces that file.
>
> These reach the installed app after `scripts/build-app.sh` and a restart of each agent's computer.

**For:** you, handing everyday tasks to an agent ("find me the phone number for...", "summarise
this document") and expecting a usable result without watching it.
**Outcome:** a look-up task such as the one that failed on 5 Oct ends with a correct answer and a
source link, with no CAPTCHA page and no manual help; and a document can go to an agent and come
back as a file.
**Appetite:** one working session for all three, in this order. Each is finished and checked
before the next starts.
**Assumptions:**
- You are the only user so far, so learning from your own real tasks beats adding breadth.
- Models will change (other machines, Vertex at work), so nothing here may depend on one model.
- Your app is running right now with an agent switched on; nothing below touches it or its data
  until you rebuild the app.

## Evidence

The first real task, "get me the phone number for kanapali parasailing on maui", was cancelled
after two minutes:

1. The agent opened a Google search and got Google's "unusual traffic" page.
2. It tried Bing, which loaded, then `browser.click` on element 12 failed with "there is no
   element 12", because the page had redrawn and lost the numbering.

Neither is the model's fault. The product has no dependable way to search the web, and its way of
pointing at page elements breaks on pages that redraw themselves.

## Now / Next / Later / Not doing

| Item | Bucket | Why |
|---|---|---|
| 1. Web research that works | **Now** | The first real task failed on it; most everyday tasks start with a search. |
| 2. Files in and out | **Now** | Without it an agent can only work on what is already on the web; "summarise this" and "give me the result" are impossible. |
| 3. Test a model connection | **Now** | Vertex at work and models on other machines are the stated next step and have never been run; this turns a failed first run into a one-click diagnosis. |
| Notification when an agent needs you or finishes | Next | Runs take minutes; valuable once tasks succeed. |
| Task list with assignment and status | Next | Chat and channels cover it for one or two agents. |
| Limit what agents can reach on your network | Next | Real gap, but no easy lever in QEMU's user networking; needs its own investigation. |
| Version control for this project | Decision | See "Decisions for you". |
| Long-term agent memory, export/backup of an agent | Later | No evidence yet. |
| Multi-user, cloud sync, mobile app, plug-in system | Not doing | One person, one Mac. |

---

## 1. Web research that works

### What is in
- A `web_search` tool: the agent gives a query and gets back a short list of results (title,
  address, snippet). It runs in the agent's own Chrome, so you can watch it, and uses a search
  page that does not challenge automated browsers.
- Element numbers that survive a page redrawing itself: a click or type on a numbered element
  still finds it after the page has changed, or says clearly that the page is different now and
  returns the fresh list in the same reply, so the agent does not need an extra step.
- When a page is a bot challenge or CAPTCHA, the tool result says so in plain words and suggests
  searching instead, so the agent does not read the challenge page as content.

### What is out (on purpose)
- Paid search APIs and API keys - comes back if the free route proves unreliable.
- Solving CAPTCHAs - that is what "Take control" is for.
- Reading PDFs and images found on the web.
- A separate "research mode" or multi-step research planner.

### Riskiest assumption
A search page exists that serves results to an automated Chrome from your network without a
challenge. Cheapest test, before building the tool: load two candidate result pages in a running
agent's Chrome and look at what comes back.

### Milestones
1. **Search spike** (30 min) - nothing for you yet; proves which search page works.
   Check: from a test agent's Chrome, a query returns at least five real results, three times in a row.
2. **`web_search` tool** - the agent can search without driving a search engine by hand.
   Check: calling the tool directly returns titles, addresses and snippets; an empty query and a
   blocked page each return a clear error.
3. **Durable element numbers** - clicking a numbered element works after the page redraws.
   Check: on a test page that rebuilds its content after loading, click by number succeeds;
   a number that no longer exists returns the fresh element list with the error.
4. **The original task** - ask a test agent for the same phone number.
   Check: the run ends by itself with a phone number and the page it came from; no challenge page
   in the tool calls.

---

## 2. Files in and out

### What is in
- Every agent has a `Shared` folder in its home directory.
- A "Files" panel on the agent page lists that folder; you can upload a file into it and download
  any file from it.
- The agent is told that your files arrive in `~/Shared` and that results meant for you belong
  there.
- Limit: 25 MB per file.

### What is out (on purpose)
- Drag-and-drop into the chat, attachments on messages - a later convenience on the same plumbing.
- Browsing the agent's whole disk from the app - "Open computer" already allows it.
- Sharing folders between agents, or mounting a Mac folder into the VM.
- Previews, versioning, deleting from the app.

### Riskiest assumption
Moving files in pieces through the existing daemon connection is reliable for files of tens of
megabytes. Test early with a 20 MB file and compare checksums.

### Milestones
1. **Upload and download through the API** - a file sent to an agent arrives intact and comes back intact.
   Check: upload a 20 MB file, read its checksum inside the VM, download it, compare all three.
2. **Files panel** - you can do the same from the agent page.
   Check: in the app window, upload a text file, see it listed, download it again.
3. **The agent uses it** - ask an agent to summarise an uploaded file and save the summary.
   Check: the summary file appears in the panel and downloads.

---

## 3. Test a model connection

### What is in
- A "Test" button next to each provider in Settings, and next to the model field of an agent.
- It sends one tiny request and reports, in plain words: reachable or not, how long it took, and
  whether the model answered a tool call (agents need that; many models cannot do it).
- Failures show the real reason: no key, not signed in to Google Cloud, wrong project, model not
  found, server not reachable.

### What is out (on purpose)
- Benchmarking or comparing models.
- Automatic model discovery for Vertex.
- Calling your Vertex project myself - that uses your work credentials and costs money, so it
  happens only when you press the button.

### Riskiest assumption
The error messages providers return are specific enough to act on. Test with the cases that can
be produced here: a wrong address, a missing key, a model that does not exist, and a working model.

### Milestones
1. **Test endpoint** - one request tells you whether a provider and model work.
   Check: against the local model server, a real model reports success and tool support; a
   made-up model, a wrong address and a provider without key each report a specific reason.
2. **Buttons in Settings and in the agent form.**
   Check: in the app window, pressing Test shows the result within a few seconds.

---

## Decisions for you
- **Version control.** This project has no git repository; one bad edit or a deleted folder loses
  everything. My recommendation: `git init` and a first commit now. I have not done it because
  you did not ask.
- **Trying Vertex.** Google Cloud credentials exist on this Mac. My recommendation: after task 3,
  enter your project in Settings and press Test yourself; I will not use those credentials.
- **Rebuilding the app.** The new features reach the app only after `scripts/build-app.sh`, which
  replaces the running copy. My recommendation: quit the app when you are at a stopping point and
  run it then.

## First step
Run the search spike in a test agent's Chrome.
