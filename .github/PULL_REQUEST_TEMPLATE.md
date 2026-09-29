## Summary

<!-- What does this PR change, and why? -->

## Affected area

<!-- middleware rules (write / shell / spawn) / whitelist / route matching /
config loading / packaging / CI / docs / tests -->

## How it was tested

<!-- e.g. `python -m unittest discover -s tests -v`, plus any live QwenPaw run -->

## Checklist

- [ ] `python -m unittest discover -s tests` passes on a bare Python ≥ 3.10
- [ ] `ruff check .` passes
- [ ] No real dispatch table, agent ids, internal system names, or people's names
      added anywhere (docs, tests, commit messages, log excerpts included)
- [ ] `routes.example.json` still installs and configures cleanly from scratch
- [ ] README and `docs/DESIGN.md` updated if user-facing behavior changed
- [ ] `version` in `plugin.json` bumped and `docs/CHANGELOG.md` has a matching entry
