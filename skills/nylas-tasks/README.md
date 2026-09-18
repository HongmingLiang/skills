# nylas-tasks

Read-only Nylas mail and calendar access for agents. The skill lists and reads
mail and events on its own, and hands everything that writes -- send, reply,
delete, move, RSVP -- to the `nylas` CLI.

Works with pi and any other client that supports the Agent Skills standard.

## Install

```bash
npx skills add https://github.com/HongmingLiang/skills -s nylas-tasks -y
```

`-s nylas-tasks` names the skill to install and `-y` skips the prompts.

`uv` and the `nylas` CLI are prerequisites, and both read the same API key
variable: [reference/setup.md](reference/setup.md).

## Development

Developed against the two official Nylas skills. From the repository that
contains this skill, install them into the project without prompts:

```bash
npx skills add https://github.com/nylas/skills -a universal -y
npx skills update -p -y
```
