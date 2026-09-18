# Contributing

Issues and pull requests are welcome here. Two rules, applied to every change:

1. **No change without a test.** A fix carries a test that fails before it and passes
   after it. A new statement in the spec carries the test that pins it, and a new target
   carries its facts and its declared gaps. A behaviour claim in a description is verified
   by running the behaviour, not by reading the types it rests on.
2. **Every claim is reproduced or cited.** A statement about the reference implementation
   names the file and line at the pinned commit; a statement about a platform (Medusa,
   Stripe, an Anthropic model) names the documentation or shows the run.

Before opening a pull request: `make lint` and `make test` are clean, and the description
says what was wrong, what changed, and how it was verified. Commits are authored by a
named person with a real email address.

The reference implementation, https://github.com/anthropics/commerce-agents, states that
it does not accept contributions. Findings about it are recorded in its pull-request list
as a public record and fixed here; nothing is sent there.
