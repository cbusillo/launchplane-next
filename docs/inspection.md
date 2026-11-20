# Inspection

`./scripts/inspect` runs PyCharm 2026.3's command-line inspector on `src`, `tests` and `scripts`. It prints `clean`, the findings, or `UNKNOWN` with the reason. Exit codes: 0 clean, 1 findings, 2 unknown.

How PyCharm behaves headless, measured on this machine:

- It opens an untrusted project in safe mode and then ignores its interpreter. Type checks and unresolved names go silent while unused variables are still reported, so a run looks clean when it is not.
- A fresh home never sets up an interpreter by itself. Before every run the script writes the project's trust and the interpreter with its library roots into the inspection home.
- PyCharm finds the interpreter by the name in `.idea/misc.xml`. The script registers the interpreter under the name already there, so a desktop PyCharm's choice and other settings in that file are kept. It writes a name only when the file has none.
- Its caches in `system` can keep a project broken after the interpreter is fixed. The script clears them before every run, which costs about 45 seconds for the four runs.
- `inspect.sh` honours only the last `-d`, so the script runs it once per folder.
- To know an answer is true, each run first inspects a canary file that calls a method `pathlib.Path` does not have. PyCharm must report that exact call, naming the class `Path`, which it can do only when it resolves the standard library. Any other report, such as an unresolved import, answers `UNKNOWN`. When the interpreter was missing in our runs, PyCharm reported nothing for the canary. So is any file PyCharm did not report analysing.
- A missing PyCharm, an unmounted home volume, or caches that cannot be cleared also answer `UNKNOWN` with the reason.
- PyCharm is told the machine has two processors, through a copy of its own VM options with `-XX:ActiveProcessorCount=2` added, and runs under `nice -n 10`. Its indexing diagnostic reports two in each of the four runs. It still keeps three threads busy: measured on November 20, 2.9 processors on average and 4 at most, where it had taken 11. The whole inspection then took 19 min 17 s.
