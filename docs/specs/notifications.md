# Know when an agent needs you

> **Status (5 Oct):** built. Checked in the running app with a test agent: the Dock badge read
> "1" while an approval waited and was empty within a second of answering it. Unit tests cover
> approval, answer, finished, failed and cancelled runs.
>
> Not checked: the bounce itself, which cannot be observed from a script. Banners were not
> built: they need a notification framework the app does not bundle and macOS permission for an
> app signed only for this Mac; the Dock badge and bounce need neither.

**For:** you, having handed a task to an agent and switched to another window, because a task
takes a minute or more.
**Outcome:** when an agent stops to ask for approval, you notice within seconds without watching
the app; and you can tell from the Dock that something is waiting.
**Appetite:** a few hours.
**Assumptions:** the app runs as its own window (the installed app). From source in a browser,
the page title carries the same information.

## What is in
- **Dock badge:** the number of agents waiting for your approval. It clears when you answer.
- **Dock bounce** when an approval starts waiting, and a single bounce when a task finishes or
  fails, but only while the app is not the frontmost application.
- **Window and page title:** "(1) Agent Office" while something waits.
- A system banner, only if macOS allows this app to post one (to be tried; see below).

## What is out (on purpose)
- Sounds, and per-agent notification settings.
- Notifications for every tool call or message.
- Email, phone or chat notifications.
- A notification history.

## Riskiest assumption
macOS lets an app that is signed only for this Mac post banners. Cheapest test: ask for
permission once from the running app and see what comes back. If it is refused, the Dock badge
and bounce are the feature, and banners are dropped rather than faked through another program.

## Milestones
1. **Attention count.** The backend knows how many agents wait for approval and when a run ends.
   Check: unit tests with a recording notifier: approval requested, answered, run finished,
   run failed, each reported once.
2. **Dock badge and bounce.**
   Check: in the running app with a test agent, trigger an approval and read the Dock badge back
   from the system; answer it and the badge is gone.
3. **Title.**
   Check: the page title shows the count while an approval waits.

## Decisions for you
- None unless banners turn out to need a setting in System Settings.

## First step
The notifier interface and its calls from the three places that already know about these events.
