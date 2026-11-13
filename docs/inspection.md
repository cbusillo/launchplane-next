# Inspection

`./scripts/inspect` runs PyCharm 2026.3's command-line inspector on `src`, `tests` and `scripts`. It prints `clean`, the findings, or `UNKNOWN` with the reason. Exit codes: 0 clean, 1 findings, 2 unknown.

How PyCharm behaves headless, measured on this machine:

- It opens an untrusted project in safe mode and then ignores its interpreter. Type checks and unresolved names go silent while unused variables are still reported, so a run looks clean when it is not.
- A fresh home never sets up an interpreter by itself. The script writes the project's trust, the interpreter with its library roots, and `.idea/misc.xml` before every run.
- Its caches in `system` can keep a project broken after the interpreter is fixed. The script clears them before every run, which costs about 45 seconds for the four runs.
- `inspect.sh` honours only the last `-d`, so the script runs it once per folder.
- To know an answer is true, each run first inspects a canary file with a known standard-library mistake. If PyCharm misses it, the answer is `UNKNOWN`. So is any file PyCharm did not report analysing.
