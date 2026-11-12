# Decision records

A decision record is the durable memory of one important decision: what was decided, why, and what it
costs. It exists so a later session or agent does not reopen a settled question or argue from a reason
already rejected.

## Where records live

One file per decision: `.janus/decisions/NNNN-title.md`, numbered in order, a short lowercase title.
`skill/templates/decision.md` is the template. A record is never edited after it is accepted: it is the
state of knowledge at its date. When a decision changes, write a new record that names the one it
supersedes and mark the old one `superseded by NNNN` in its status line — the only line that ever
changes after acceptance. The chain of supersessions is the decision's history.

## What counts as important

A record is for a decision that will constrain later choices:

- owner rulings — any rule from the owner that governs future work;
- budgets and caps — what may be spent, where, and the ceilings;
- gate policy — what must pass before work lands, and what counts as a known failure;
- hosts — which machines take which work, and any limit placed on them;
- publishing — what leaves the project, and where it goes;
- routing — which model or vendor is used for which work.

A routine acknowledgement is not a decision; it is the orchestrator doing its job. The test is: if
someone asked "why is it like this?" a month from now, the answer is a record, not a line on the board.

## The index

The records have a generated index (`.janus/decisions/INDEX.md`), one line per record — number, title,
date, status — newest first, beside the reports index. It is not written by hand; it is regenerated from
the files, like the Live state of the handoff.

## How the orchestrator uses them

The index is injected at session start, with the session memory, the handoff, the reports index and the
peers. Before deciding anything a record touches, the orchestrator reads that record. A record is binding
until a newer record supersedes it, and the newer one inherits its context. Records are required for
decision-class rulings: when the owner or the orchestrator settles anything in the list above, it is
written down at once.

## Commands

`janus decide "<title>" --by owner|orchestrator --decision "<one sentence>" [--why "<ruling>"] [--context "<what forced it>"]
[--options "<option and trade-off>" ...] [--consequences "<what it requires>"]` writes a numbered record from the
template and regenerates the index; `--supersedes <n>` marks the record it replaces (its Status line only) and links the
chain; `--instruction <beads id>` links the owner instruction the decision settles. `janus decisions [--grep <regex>]`
lists the records, newest first, or the lines that match. The format above is what those commands read and write.

## Difference from a project's own decision records

A project may keep its own records — for product or architecture decisions — in its repository. Those
belong to the project: they describe the product and travel with its code and history. janus's records are
about the orchestration: how work is run, spent, gated, placed and published. They live in the project's
state directory (`.janus/`), beside the handoff and the session memory, not in the product's tree. The two
sets do not merge; where an orchestration record depends on a product decision, it links to that decision
rather than restating it. The map of all the stores is `docs/KNOWLEDGE.md`.
