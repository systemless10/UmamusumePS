# server/fixtures

Capture data the **server itself** reads at runtime. Tracked in git, unlike the
raw corpora at the repo root (`captures/`, `20260717_124934_06d26c/`), which are
multi-gigabyte working data and are gitignored.

| Path | Read by | Was |
| --- | --- | --- |
| `capture_06d26c/` | `app/fixtures.py` (`CAPTURE_DIR`) -- the fixture-replay source for endpoints with no real handler yet | `<repo>/20260717_124934_06d26c/` |
| `captures/20260818_122351/0027_idle_single_mode_pre_start.json` | `app/handlers/idle_single_mode.py` (`_PRE_START_CAPTURE`) -- the 8 preset deck agendas | `<repo>/captures/...` (same relative path) |

Files under `captures/` here keep their original session-directory path so the
citations scattered through `app/handlers/` still line up.

If a handler starts depending on another file from the raw corpora, copy it in
here (keeping its session path) rather than reaching into `captures/` -- that
directory is not present on a fresh checkout. The `tools/` scripts are the
exception: they analyse the corpus in place and are expected to be run only on a
machine that has it.
