# Development and verification

The supported hardware environment is Windows. Python 3.10+ is required;
local regression checks use Python 3.12. Keep the root Python modules together.

## Synthetic regression tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests build artificial records and mock USB access. They do not open a device,
need private capture fixtures, or require TI/Infineon GUI software.
GitHub Actions repeats them on Windows with Python 3.10 and 3.12.

## Optional hardware and GUI interoperability checks

Keep GUI ownership separate from CLI capture. Record to a fresh prefix under
`captures/`, stop capture, then open the exported CSV/ccgx3 in Utility 4.2.
Check PD values, packet counts, timestamps and waveform units. Capture formats
retain raw records so comparisons do not depend only on rendered table values.

Java helpers in `tests/` are manual checks against the installed proprietary
Utility classes; they are not part of CI and no vendor JAR is distributed.
Use a JDK and supply your own installation's plugin JAR on the classpath.

`InspectCapture.java` prints serialized packet fields and extracts raw records.
It requires an input archive and an output binary path. For example:

```powershell
$utilityJar = 'C:/path/to/com.cypress.ezpdanalyzer.ui_VERSION.jar'
javac -cp $utilityJar tests/InspectCapture.java
java -cp "tests;$utilityJar" InspectCapture captures/session.ccgx3 captures/session.extracted.records.bin
```

`CheckScope.java` is a historical assertion helper for a specific artificial
single-sample waveform (amp=-1, CC1=123, CC2=456, volt=4095,
timestamp=4294967306); it is not a validator for arbitrary recordings.
The helpers require Utility 4.2 classes. No recorded measurements or vendor
classes are included.

## Local files and publishing

Store measurements and generated reports under Git-ignored `captures/`.
Virtual environments, compiled Python/Java files and investigation notes are
not publication artifacts. Check staged paths and `git diff --check` before
committing. Hardware validation is separate from synthetic CI: passing unit
tests does not establish timing accuracy or hardware interoperability.

Preserve original capture data during reanalysis and use a new output prefix.
Waveform settling results are observations rather than compliance verdicts.
