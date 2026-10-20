# CppDepend tools

`imdepend` is a Python command-line tool for CppDepend workflows. Version 0.1.0
validates packaging and distribution: it supports only `--help` and `--version`.
CppDepend installation commands will follow in a later release.

The planned package, profile, and launch workflows are described in the
[CppDepend installation guide](docs/cppdepend-install.md).

## Install from the internal share

Prerequisites: Python 3.12 and uv. In PowerShell, run:

```powershell
uv tool install --python 3.12 --no-python-downloads --offline --no-index "\\innovmetric.com\innovdfs\software\cppdepend\cppdepend-tools\0.1.0\im_cppdepend_tools-0.1.0-py3-none-any.whl"
imdepend --version
imdepend --help
```

The share's UNC path works even if `X:` is not mapped. If PowerShell cannot
find `imdepend` after installation, run `uv tool dir --bin` to find uv's
launcher directory. Add it to your user PATH and open a new terminal. You can
also run `uv tool update-shell` to ask uv to update PATH for you.

## Uninstall

Remove the installed tool with uv:

```powershell
uv tool uninstall im-cppdepend-tools
```

Use the distribution name `im-cppdepend-tools` here, even though the command
you run is `imdepend`. This removes uv's tool environment and the `imdepend`
launcher. It does not delete a downloaded wheel from the shared drive.

## Build and release

Run from a checkout on Windows with Python 3.12 and uv:

```powershell
py -3.12 scripts/release.py
```

The script builds the source archive and wheel with Hatchling through uv. It
installs the wheel into a temporary uv tool store, checks `--help` and
`--version`, confirms that an unimplemented subcommand fails, and prints the
wheel path and SHA-256. It does not change an existing uv tool installation.

When ready to publish, a developer runs:

```powershell
py -3.12 scripts/release.py --publish
```

That command rebuilds and tests the wheel, then copies it to the versioned
folder on the internal share. It checks SHA-256 after copying and refuses to
replace a published wheel with different contents. GitHub CI can be added
after this local workflow is established.
