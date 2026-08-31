"""CLI subcommand modules.

Each module here owns one user-facing subcommand or shortcircuit handler that
``cli.py`` dispatches to. Keeping them as plain functions (not classes) matches
the procedural CLI design — argparse is the entry point, these are the handlers.
"""
