"""Specialist agents bound into the supervisor graph.

Each agent is deterministic orchestration around the model, not a free-roaming
tool-calling loop:

* :mod:`app.agents.analyst` turns a question into one guarded SQL query, runs
  it, and summarises the rows.
* :mod:`app.agents.research` plans web searches, runs them, and synthesises an
  answer with citations.
* :mod:`app.agents.report` gathers whichever evidence the request asks for and
  writes a Markdown report from it.

Determinism is the point. The supervisor already decided *which* capability the
request needs, so each agent knows its tools; letting a model choose tools
inside the agent would add a second, untestable decision to every request
without adding information.
"""
