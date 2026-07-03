# Team Tasks

We need a small web app our team uses to track shared tasks.

- Anyone on the team can see the task list, add a task, and mark one done.
- Everyone sees the same list. Several people use it at once, and we may run more than
  one copy of the app for reliability — so the data must live in one shared place, not
  inside any single app process.
- Tasks must survive restarts and redeploys; losing the list is not acceptable.
