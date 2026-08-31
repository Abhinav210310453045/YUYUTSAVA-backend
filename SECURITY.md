# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/Abhinav210310453045/YUYUTSAVA-backend/security/advisories/new)
— the *Security* tab → *Report a vulnerability*. That opens a private thread
visible only to the maintainer.

Useful things to include: what an attacker can do, the conditions required, and
a reproduction if you have one. Please say whether you intend to disclose
publicly and on what timeline.

This is a single-maintainer alpha project, so please be realistic about
response times. You will get an acknowledgement; a fix depends on severity and
on how much of the design has to move.

## Supported versions

Only `main` is supported. There are no maintained release branches yet, and
no backports.

---

## Threat model — read this before reporting

YUYUTSAVA is an agent that **runs shell commands and reads and writes files on
your machine, by design**. A large class of behaviour that would be a
vulnerability in ordinary software is the entire point of this one.

**Not vulnerabilities:**

- The agent executing a shell command you asked it to execute.
- The agent reading or writing files inside its workspace.
- A model being talked into doing something unwise by the user driving it. If
  you instruct the agent to delete your files, it may well try.
- The daemon's HTTP API being unauthenticated on loopback. That is documented
  and deliberate — see below.

**Genuinely interesting, please report:**

- **Escaping the permission model.** A path that reaches a filesystem or shell
  operation without passing through the TaskRunner's zone classification, or a
  way to make a `SYSTEM_CRITICAL` path classify as `WORKSPACE`.
- **Sandbox escape.** Getting out of the Docker execution mode onto the host.
- **Indirect prompt injection with real consequence** — content in a file, web
  page or tool result that causes the agent to take a privileged action the
  user did not ask for and would not have approved. This is the most valuable
  category and the least explored.
- **Consent/allowlist bypass.** Making a call that should have prompted proceed
  silently, or persisting a broader grant than the user approved.
- **Credential leakage.** API keys reaching logs, traces, artifacts, the
  transcript store, or a model prompt they were not meant to reach.
- **MCP boundary problems.** A scoped-out MCP server's tools reaching an agent
  that should not see them.
- **Remote reachability.** Anything that lets a non-loopback origin reach the
  daemon API without `YUYUTSAVA_API_TOKEN`.

---

## Operational notes

**The daemon binds loopback and is auth-exempt there.** Binding it to any other
interface requires `YUYUTSAVA_API_TOKEN`. Do not expose the daemon to a network
you do not trust; it is not hardened for that, and the API can drive an agent
with filesystem and shell access.

**Docker execution is the stronger boundary.** `--execution docker` with
`--docker-network none` is meaningfully safer than host execution for
untrusted work. Host execution relies on the permission model alone.

**Secrets live in `.env`,** which is gitignored. `.env.example` ships with empty
placeholders. If you fork or publish a branch, check you have not committed a
populated `.env`.

**Run history is real data.** Checkpoint and transcript stores hold your actual
prompts and model output. `.langgraph_api/` in particular is gitignored for
exactly this reason — it has been committed by accident before. Do not commit
it, and scrub logs before pasting them into an issue.
