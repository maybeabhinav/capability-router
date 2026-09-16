# Contributing

Use Python 3.10 or newer. Runtime code must remain standard-library only.

Before you submit a change, run:

```sh
python3 -m unittest discover -s tests -v
python3 tests/verify_sdist.py
python3 -m compileall -q capability_router tests
```

Add tests at the public CLI or MCP boundary. Do not add credentials, personal
paths, machine names, runtime catalogs, audit logs, or generated artifacts.
