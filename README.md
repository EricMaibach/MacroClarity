# MacroClarity

**An experiment in AI-driven development.**

I wanted to answer a few questions for myself: how much of the development process can AI actually handle? As a codebase grows past toy size, can AI still manage it? What practices make it work?

So I built a real product to find out — a macro financial dashboard that collects market data, uses AI to analyze it, and explains it in terms an average individual investor can follow. The product is genuine and it works. But the reason it exists was to run the experiment, and this README is about what I learned.

## What happened

Nine AI agents ran the product development lifecycle — research, strategy, product management, design, engineering, and QA — coordinating through GitHub Issues and Discussions. I approved features and merged pull requests. Everything in between was automated.

Over roughly ten weeks it shipped **181 merged pull requests across 71 user stories and 16 release phases**, including real domain complexity: a four-dimension macro regime engine, Taylor-rule policy scoring, three independent recession models, and FinBERT sentiment analysis over SEC filings.

However, it also produced a codebase that is not well structured — the main application file grew from 1,400 to over 6,000 lines and no agent ever proposed breaking it up — and it went off on some wrong tangents that had to be walked back.

## How it worked

```
IDEATION   Researcher ┐
           Designer   ├─→ CEO decides ─→ PM writes feature ─→ ◆ human approves
           Engineer   ┘

BUILD      QA test plan ─→ Engineer ─→ Designer review ─→ QA verify ─→ PR ─→ ◆ human merges
                              ↑              │               │
                              └──────────────┴───────────────┘
                                   rejections loop back

           ◆ = the only two points where a human was required
```

GitHub labels were the entire state machine — no orchestrator, no queue, no message bus. Each agent woke on a timer, asked GitHub what was in its queue, did exactly one thing, moved the label, and exited. The pipeline advanced because the next agent's poll found different state.

Two details mattered more than I expected. Agents reviewing each other will argue indefinitely, so rejection cycles were capped and escalated to me on the third. And because only a human could merge, an open pull request stalled the entire pipeline until I dealt with it — the approval gate was real backpressure rather than a policy.

## What I learned

- Splitting work across agents with different perspectives creates a genuinely adversarial review process, and review quality improved because of it.
- Divide memory based on scope:
    - **Task memory** scoped to the task, stored as comments on the GitHub issue. It can be shared across the agents and humans working on that task, and it records a history of what was done and why.
    - **Role memory** scoped to the role, so different instances of a role share what they know, but memory does not leak to other agents and erode the adversarial review effect.
- Having the engineer agent generate screenshots of the UI whenever it made UI changes, so the designer agent could review them, dramatically decreased UI issues.
- AI never got to the point where it could not manage the code, but it did not do a great job of organizing and structuring it — it tends to just add. The same thing happened with the UI, until a dedicated designer agent working from a documented design framework turned that around. An architect agent with a defined architecture and philosophy would likely do the same for code structure, though I expect it would still need more human review than the design side did.
- The framework for automating the agents needed to be better — tighter security restrictions and real logging.
- AI needs to amplify your expertise, not replace it. Because I am a novice at macro finance, AI was able to take me fairly far down some bad paths on features and analysis techniques that sounded really good but were actually invalid.

## The product

A macro dashboard for individual investors: 50+ indicators with historical percentile context, AI-generated daily briefings, portfolio analysis, and a chat interface for asking questions about the data. Python, Flask, SQLAlchemy, Docker, deployed via GitHub Actions.
