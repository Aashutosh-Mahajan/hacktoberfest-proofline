# Proofline: Project Guide (plain-language explanation + demo script)

> Proofline is an AI on-call engineer for payments. When payments start failing, it finds out why, **proves** its answer, **shows what its fix will do before anyone clicks**, and acts only after a human says yes.

---

## 1. The problem, in simple words

Imagine millions of people paying through UPI. Suddenly, out of every 100 payments, 10 fail.

An engineer is woken up. For the next 15-30 minutes they:
- stare at charts, logs and deploy history,
- ask "did we release something? is a bank down? is it just users typing the wrong PIN?",
- guess, then try a fix, then hope.

Every minute costs real money and real trust. Tools exist that tell you **that** something broke. Few tell you **why**, and none let you **see the future of a fix before you apply it**.

There is a second problem: **AI that sounds confident but is wrong.** If an AI says "it was the latest deploy" and the engineer believes it, they roll back the wrong thing and the outage gets longer.

## 2. What Proofline does (one paragraph)

Proofline watches payment success rates 24x7. When they dip, it opens an incident on its own, investigates like a senior engineer (checking banks, deploys, traffic, logs), writes down its theories and rules them out one by one, then states a root cause. A **rules engine** (plain code, not AI) checks that claim against the real data. If the claim is weak, it is **rejected** and the AI must try again. Once a claim passes, Proofline **simulates the proposed fix in a copy of the system** and shows a predicted recovery line on the chart. The human clicks **Approve**, the real line follows the predicted one, and the screen shows how much money was saved.

## 3. The core idea: three gates

> The AI **proposes**. Code **decides**. A human **approves**.

| Gate | Question it answers | How |
|---|---|---|
| 1. Evidence gate | "Is every claim backed by real data?" | Every claim must point to evidence IDs that exist (a deploy record, a metric, a log, a bank bulletin). |
| 2. Logic gate | "Does the story make sense?" | A deterministic verifier checks timing, scope and cause/fix consistency (details in section 5). |
| 3. Outcome gate | "Will the fix actually work?" | The fix is run first in a **digital twin** (a forked copy of the simulator). Only a fix the twin says recovers can be offered. |

## 4. Key features (explained simply)

1. **Auto-detection.** No one clicks anything. When the 30-second success rate falls 5 points below the 5-minute normal for 10 seconds, an incident opens. The start time is back-dated to when the dip really began.
2. **Technical vs business decline triage.** Failed payments are split into two kinds:
   - *Technical:* timeouts, bank errors. Something is broken. Fix it.
   - *Business:* wrong PIN, daily limit exceeded, low balance. The user's side, not a fault.
   Proofline refuses to "fix" what is not broken. In the cashback-surge scenario it says "infrastructure is healthy, send communications", not "roll back".
3. **Hypothesis board.** Visible theory cards (bad deploy, bank outage, traffic spike, business decline) move through *open, supported, ruled out, confirmed*, each with a one-line reason.
4. **The verifier (our trust engine).** Eleven deterministic checks, for example:
   - Did the deploy happen **before** the dip? (A deploy that landed 41 seconds after is cleared.)
   - Is the failing path actually affected by that deploy?
   - Are failures concentrated in the blamed bank, and are other banks healthy?
   - Does the decline type match the cause (business cause means mostly business declines)?
   - Does the proposed action fit the cause?
   - Did the twin predict recovery?
   Rejections are **shown on screen in amber**. We show them proudly, because that is the system working.
5. **Fix Preview (digital twin).** Before approval, the fix is run in a copy of the simulator with and without the action. The chart shows a ghost "predicted recovery" line. After Approve, the screen shows "predicted 6 s, actual 8.8 s". It is checked openly, not hidden.
6. **Human approval with safety.** The AI can only propose. Approval needs a single-use token tied to that incident and action, which expires in 5 minutes. Actions come from a fixed whitelist: roll back a deploy, fail over a bank, circuit breaker, scale gateway, notify only.
7. **Rupees saved counter.** Shows the loss avoided compared with a 25-minute manual response. The assumption is always labelled on screen.
8. **Chaos Mode (blind test).** A judge presses a button. A random fault is injected and the scenario name is **hidden** from the presenter. After the agent answers, the truth is revealed with a tick or cross and added to a scoreboard. This is how we show the demo is not scripted.
9. **Evidence you can click.** Chips open the deploy diff, logs or the bank's status bulletin image, including whether the bulletin's claim matches telemetry.
10. **Stakeholder comms.** Drafts for internal, merchant and customer audiences, in **English and Hindi**, for a human to approve. Nothing is actually sent.
11. **Auto postmortem.** A timeline plus a short narrative of cause, impact, detection and resolution times, and predicted vs actual recovery.
12. **Voice / text war-room.** Ask follow-ups such as "BANK_Y pe koi asar hua?" in English, Hindi or Hinglish. The agent runs read-only tools and answers. (Text input works today. Native Gemma audio input is not wired in; push-to-talk uses the browser fallback.)
13. **Honest eval board.** Reproducible accuracy numbers across many randomised runs, with a "verifier on vs off" comparison to show what the verifier adds.
14. **Replay mode.** Replay a recorded run offline as a demo fallback.
15. **Runs offline.** All data is local and synthetic. No cloud is needed for the product itself.

## 5. The scenarios we can inject

| ID | What happens | Correct answer | What is tricky |
|---|---|---|---|
| S1 | A deploy cuts BANK_X's timeout from 3000 ms to 800 ms | Roll back deploy #482 | Link a config change to one bank's timeouts |
| S3 | BANK_X has an outage **and** an unrelated deploy (#483) lands 40 s after the dip | BANK_X outage, and **clear the deploy** | The decoy. Naive AI blames the latest deploy |
| S5 | A cashback campaign pushes users past limits | Business declines; **do not roll back**, notify | Nothing is broken |
| S0 | Normal traffic | No incident | No false alarms |
| S2 / S4 / S6 | Bank outage with bulletin, traffic spike, compound fault | | Planned, not built in this version |

## 6. How it works under the hood (simple map)

```
 Simulated payments (gateway -> orchestrator -> UPI -> BANK_X / BANK_Y / BANK_Z)
            |
      Detector (math, no AI) -> opens incident, splits technical vs business
            |
      AI investigator (Gemma) uses tools: success-by-bank, decline breakdown,
      logs, deploys, traffic, bank bulletin
            |
      Verifier (rules)  --reject--> AI tries again (max 3 times)
            | pass
      Digital twin: fork the simulator, try the fix, predict recovery
            |
      Human clicks Approve (single-use token)
            |
      Fix applied -> predicted vs actual shown -> money saved -> comms + postmortem
```

Everything streams to the dashboard live over a WebSocket.

## 7. Web dashboard workflow (what the screen does, step by step)

**Layout:** a top bar, a big live chart, bank tiles and a decline donut, then the agent panel and the outcome panel.

| Step | You see on the dashboard | What is happening |
|---|---|---|
| 1. Healthy | Success rate about 99%, "All systems nominal. Proofline is watching." | Baseline traffic, no incident |
| 2. Inject a fault | Pick a scenario in the control bar (or press **Chaos**), then Inject | The simulator starts the fault |
| 3. Dip | The chart line drops. A red incident band appears; "Rupees at risk" rises | Detector opens the incident |
| 4. Triage | Decline donut shows e.g. 95% technical. Blast radius appears | Breakdown and impact computed in code |
| 5. Investigation | Tool calls stream in. Hypothesis cards flip to *ruled out* or *supported* with reasons | The AI gathers evidence |
| 6. Verifier | Amber row if a claim is rejected ("deploy landed after the dip"), then a green pass | Rules check the claim |
| 7. RCA card | Cause, confidence, blast radius, **evidence chips**, "ruled out" list | A verified root cause |
| 8. Fix Preview | A dotted ghost line shows predicted recovery, e.g. "back to normal in about 6 s" | The twin tests the fix |
| 9. Approve | Click **Approve** (or Reject) | Single-use token consumed |
| 10. Recovery | The real line climbs and follows the ghost line. Card shows "predicted 6 s, actual 8.8 s" | Fix applied and measured |
| 11. Money | Rupees saved counter appears in the top bar | Loss avoided vs 25-minute manual response |
| 12. Wrap-up | Comms drafts (EN/HI tabs) and a Postmortem modal | Generated automatically |
| 13. Chaos reveal | Scenario name revealed with a tick or cross, and the scoreboard updates | Honest blind test |
| 14. Eval Board | Accuracy table and "verifier on vs off" bars | Reproducible proof |

Other controls: the theme toggle (dark / high-contrast projector), the model health badge, the connection indicator and the hidden demo controls (`Shift+D`).

## 8. Honest limitations (say these first; judges trust it)

- Payment traffic is **simulated**. The agent and verifier are real.
- Bank names are synthetic (BANK_X/Y/Z) and decline codes are synthetic, not real NPCI codes.
- Fixes are applied to the simulator, not real infrastructure.
- Native audio and image input to Gemma are not wired in yet (browser speech fallback and text are used).
- Before presenting, check which model is driving the investigation (the header badge shows `gemma4:e4b` or `scripted`). **Do not tell judges Gemma is reasoning live unless the badge says so and a run has completed with it.** On our 6 GB GPU the 9.6 GB model failed to load during testing, so this must be confirmed first.

---

## 9. Demo script (about 4 minutes)

Stage notes are in *italics*. Speak the plain text. Adjust lines marked **[CHECK]** to what is actually true on the day.

### Opening (20 s): the hook

"Every time UPI payments dip, someone gets paged at 3 a.m. and spends the next thirty minutes **guessing**. Guessing costs lakhs per minute. And today's AI tools make it worse: they sound confident, and you can't tell when they're wrong.

We built **Proofline**: the on-call engineer that **never asks you to trust it**. It *proves* every answer, *previews* every fix, and acts only when a human says yes."

*Show the dashboard, healthy: green line, "All systems nominal."*

### Scene 1 (60 s): a bad deploy

"Everything is healthy. Traffic is simulated, but the agent you're about to see is real. Now I break something."

*Select S1 and inject.*

"Watch the chart. Success rate drops. Nobody clicked anything. **Proofline opened the incident itself** and back-dated the start to the exact second the dip began."

*Point to the donut.*

"First thing it does: split the failures. Ninety-five percent are **technical**: timeouts, not wrong PINs. So something is genuinely broken."

*Point to hypothesis cards and the tool stream.*

"These cards are its theories: bad deploy, bank outage, traffic spike, business decline. Watch them get **ruled out one by one, each with a reason**. This is investigation, not autocomplete."

### Scene 2 (40 s): the verifier

*If the amber rejection appears:* "See the amber row. The AI made a claim and our **rules engine rejected it**. That is not a bug. That is the product. The AI proposes, **code decides.**"

*If it passes first time:* "Eleven deterministic checks just passed: the deploy landed *before* the dip, it sits on the failing path, failures are concentrated in one bank, the others are healthy."

"Every claim links to evidence you can click." *Click an evidence chip; show the deploy diff.* "Here is the exact line: timeout cut from 3000 to 800 milliseconds."

### Scene 3 (45 s): see the future

"Here is the part no one else has. Before we touch production, Proofline runs the fix in a **digital twin**, a forked copy of the system."

*Point to the ghost line.*

"This dotted line is the **predicted recovery**. The AI isn't asking for trust. It's showing us the future. I click **Approve**."

*Click Approve; let the real line follow the ghost.*

"The real line follows the prediction. Predicted six seconds, actual under nine. We even show the gap, because we'd rather be honest than perfect." *Point to the rupees saved counter.* "And that's the money saved versus a twenty-five-minute manual response. The assumption is labelled right there."

### Scene 4 (45 s): the decoy (shows real intelligence)

"Easy case. Now the one that fools most AI."

*Select S3 and inject.*

"A bank is down, **and** at almost the same moment an unrelated deploy goes out. The lazy answer is 'blame the latest deploy.'"

*When the RCA appears, point to the ruled-out list.*

"Proofline cleared that deploy because it landed **41 seconds after** the dip started. It can't be the cause. It blamed the bank and recommended failing over to a healthy one."

### Scene 5 (30 s): knowing when not to act

*Select S5 and inject.*

"This time a cashback offer pushes users past their daily limits. Success rate drops, but nothing is broken. Proofline says: **do not roll back**. It recommends notifying stakeholders instead, and drafts the message in **English and Hindi**." *Open the comms panel, flip to Hindi.* "Knowing when *not* to fix is what separates an on-call engineer from a script."

### Scene 6 (60 s): Chaos Mode, the proof it is not scripted

"You may be thinking: 'you rehearsed this.' So let's remove me from the equation. *[Name], would you press this button?*"

*Judge presses Chaos. Scenario name stays hidden.*

"I don't know what just broke. Neither does the AI. It only sees telemetry."

*Wait for the agent to finish.*

"Reveal." *Show tick or cross, update scoreboard.* "Ground truth versus what the agent concluded, on the record." **[CHECK: if it is wrong, say: "That's the honest part. The verifier stops wrong answers from reaching you, and here is why we show the scoreboard."]**

### Scene 7 (20 s): voice / text war-room (optional)

*Type or speak:* "BANK_Y pe koi asar hua?"

"Ask in Hindi, Hinglish or English. It runs live queries and answers from real data." **[CHECK: say "push-to-talk" only if audio works; otherwise "ask in text."]**

### Closing (30 s): proof and the model line

*Open the Eval Board.*

"We don't ask you to believe us. Here are the numbers across randomised runs, including a run with the **verifier turned off**, so you can see exactly what it adds." **[CHECK: only quote numbers actually on the board, rerun the eval first.]**

"It runs fully on a laptop with **Gemma**, so sensitive payment data never leaves the building. **[CHECK: say this only if the Gemma badge is live.]**

Proofline: **it doesn't just find the problem. It proves it, previews the fix, and waits for you.** Thank you."

---

## 10. Likely judge questions and honest answers

- **"Is the traffic real?"** No. It is a deterministic simulator. We say that up front. The agent, verifier and twin logic are real, and Chaos Mode shows it isn't scripted.
- **"What if the AI is wrong?"** The verifier rejects weak claims, the twin must predict recovery, and a human approves. Budgets force an honest "inconclusive" instead of a guess.
- **"Why a digital twin?"** So an engineer sees the effect of a fix *before* acting, and the prediction error is measured in the open.
- **"Why Gemma?"** Local and private (payment telemetry is sensitive), native tool calling for the investigation loop, and multilingual output for Hindi comms. **[CHECK: only claim what is wired in and verified.]**
- **"How is this different from existing AI SRE tools?"** Three gates (evidence, logic, outcome), the fix preview, technical-vs-business triage and public eval numbers with a verifier ablation.

## 11. Pre-demo checklist

- [ ] Backend (`:8000`) and frontend (`:5173`) running
- [ ] Header badge: confirm whether it shows `gemma4:e4b` or `scripted`, and run one full S1 pass with it
- [ ] Run S1, S3, S5 three times each; confirm they pass
- [ ] Rerun the eval (`cd backend && python -m mayday.eval.run --runs 10`) so the Eval Board has real numbers
- [ ] Projector theme readable from the back row
- [ ] Backup: record a good run, and keep `?mock=1` open in a second tab as a fallback
