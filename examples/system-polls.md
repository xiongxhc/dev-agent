# Team Polls

We want a small internal web app where teammates can create quick polls and vote on them.

- Anyone can create a poll: a question plus two or more answer options.
- Anyone can see the list of polls, with the current vote count for each option.
- Voting is one click on an option; the displayed counts update.
- Polls and votes must survive a restart — nothing lives only in memory.
- Expect several teams to use it at once, so keep the data in a proper database.
