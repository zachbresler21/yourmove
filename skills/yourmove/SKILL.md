---
name: yourmove
description: Use when keeping the yourmove board true. That means starting work that will outlive the session, opening or merging a PR, deploying, getting blocked on the owner, answering a comment from the board, or when a hook says the board is out of date or a card is stale. Covers the seven states, writing a card's next move, needs-you asks with choices, and the `yourmove` CLI.
---

# Keeping the board true

The board is how the owner sees every piece of work without asking. Each card is one piece of
work, and it sits in the column that names its next move. You keep your card true. The owner
reads it and does not ask you.

## The states

| State | Means |
| --- | --- |
| `needs-you` | The next move is the owner's: a decision, a merge, an approval, a verdict. |
| `planning` | Planned and not started. |
| `in-progress` | A session is building it. |
| `in-review` | A PR is open, waiting on review or checks. |
| `needs-testing` | Not merged. It has to be tried on a device before it can merge. |
| `needs-deploy` | Merged into main. Waiting on a deploy, a build, a store review or a switch. |
| `live` | In users' hands, or a test that is running. |

- Blocked on the owner beats every other state. Move the card to `needs-you`.
- Merged into main is `needs-deploy`, always. A check owed after the work ships goes in `next`.
  It does not move the card back.
- `needs-testing` only ever holds work that is not merged.

## When to touch the board

- **Starting work that will outlive the session:** `yourmove add --here ...` (`--here` records this worktree).
- **The work changes state** (PR opened, merged, deployed, blocked): `yourmove set <id> --state ... --next "..."`.
- **You checked a card and it still holds:** `yourmove touch <id>`. A card untouched for five days shows as stale.
- **The work was dropped, or has been live a week:** `yourmove rm <id>`.
- **The owner commented on a card:** answer in your reply, then `yourmove reply <comment id> "<what you did>"`.
- **The work has a ticket in the team's tracker** (Linear, Jira): record it with `--ticket SUN-142`.

Touch only your own cards. Other sessions own theirs, and the session-start briefing tells you
which work is in flight elsewhere. The board syncs PR states from GitHub on its own, but it
cannot write `next` for you.

## Tickets

The board is the owner's. The team's tracker is everyone's, and it keeps itself in step with
GitHub: a PR whose branch name or title holds the ticket ID gets linked to the ticket, and the
tracker moves the ticket when the PR opens and merges. So when a card has a ticket, put its ID in
the branch name (`sun-142-gift-cards`) and the PR title (`SUN-142: Gift cards`). Never move the
ticket by hand to match the board.

## Writing `next`

`next` is the next move in one or two short sentences, under 280 characters. The CLI refuses longer.

- Say the move, not the history. "Waiting on checks." beats a log of what you tried.
- Name who acts when it is not the owner: `--waiting "Sam"`.
- Detail belongs in the PR description, where it stays current. The card points; the PR explains.
- On a `live` card, say what to watch and when to read it: "Read paywall conversion on Oct 27."
- No secrets and no user data. The board is plain text on disk.

## Asking the owner

A `needs-you` card's `next` is the exact thing for the owner to say or pick. Make the answer
cheap: give the decision, then offer the answers as choices. Each one is a button on the board,
and a press reaches you as a comment.

```sh
yourmove set shop-headline-test --state needs-you \
  --next "The payments provider stopped the headline test Oct 2. Conclude it here too?" \
  --choice "Conclude it" --choice "Ask Sam first"
```

Write each choice as a reply the owner would type, so it reads as an instruction when it reaches you.

## Commands

```sh
yourmove ls [--state S] [--project P]   # the board, column by column
yourmove show <id>                      # one card, its choices and comments
yourmove add --project P --title T --next N [--state S] [--here] [--pr N] [--ticket T] [--first]
yourmove set <id> [--state S] [--next N] [--title T] [--pr N] [--ticket T] [--here] [--waiting W] [--choice C] [--clear FIELD]
yourmove touch <id> | rm <id>
yourmove reply <comment id> "<text>"
yourmove lint                            # after editing cards.jsonl by hand
yourmove log --days 7                    # what moved, for a weekly review
```

Order inside a column is priority. `add` puts a card last in its column, and `--first` puts it at the top.
