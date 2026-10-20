# Install and use CppDepend with imdepend

`imdepend` manages portable CppDepend ZIP distributions on Windows. This guide
describes the planned installation commands. The current `0.1.0` release is a
packaging check: it supports only `imdepend --help` and `imdepend --version`.
The commands below become usable when the installer release is published.

## Packages and profiles

A **package** is one registered vendor ZIP. It has a readable name called a
*slug*, a description, and a SHA-256 hash that identifies its exact contents.
The slug can be corrected later; the archive hash does not change.
The vendor version alone is not enough: two ZIPs can both be labeled
"CppDepend 2026.2" while containing different builds. Register a ZIP once;
developers can then install it by slug without finding the original download.

A **profile** is a named local CppDepend installation, such as `good`, `latest`,
or `ci`. Each profile has its own extracted program files. Different profiles
can hold different packages and run side by side. A profile keeps its current
package and, after an update, one previous package for rollback.

Installing the Python tool and installing CppDepend are separate actions:

```text
uv tool install ...                         installs the imdepend command
imdepend profile install latest 2026-2-fix  installs CppDepend into a profile
```

The tool uses `%LOCALAPPDATA%\imdepend\profiles` for local profiles and
`%LOCALAPPDATA%\imdepend\config.toml` for path overrides. These files are
separate from uv's tool environment, so upgrading `imdepend` does not replace
your CppDepend installations. `imdepend` does not alter CppDepend's shared
history and preferences in `%APPDATA%\CppDepend` or manage license activation.

## Shared package repository

Registered vendor packages live at:

```text
\\innovmetric.com\innovdfs\software\cppdepend\cppdepend-packages\
```

This is beside `cppdepend-tools`, the folder that distributes `imdepend`
wheels. The two folders contain different products:

```text
\\innovmetric.com\innovdfs\software\cppdepend\
  cppdepend-tools\                  imdepend release wheels
  cppdepend-packages\               registered CppDepend ZIPs
    packages.toml                   slug-to-SHA-256 index
    2026-2-fix-a1b2c3d4e5f6.zip    immutable vendor archive
    2026-2-fix-a1b2c3d4e5f6.toml   its description and metadata
```

The 12 hexadecimal characters in the filenames are a readable prefix of the
full SHA-256 recorded in the manifest and index. The filenames remain stable
if a package slug is corrected later. The example names and hash above are
illustrative. Shared-folder permissions control who can register packages.
Developers with read access can install registered packages.

The default locations work without a setup step. To check them or choose other
locations, use `imdepend setup`; consult `imdepend setup --help` for the
available path options. An installed profile can still be inspected or launched
when the share is unavailable. Registering a ZIP or installing a different
package requires access to the shared repository.

## Register a vendor ZIP

A developer with write access registers an offline CppDepend ZIP once:

```powershell
imdepend package register "C:\Downloads\CppDepend_win_x64_2026.2.zip" --slug 2026-2-fix --summary "2026.2 build with fix" --vendor-version 2026.2
```

`imdepend` checks the archive, shows the proposed manifest, and asks for
confirmation before adding it. `--vendor-version` is optional; the archive
hash remains the exact package identity. For noninteractive registration,
provide `--slug` and `--summary` explicitly and add `--yes`.

```powershell
imdepend package list
imdepend package show 2026-2-fix
```

Registering identical ZIP contents again reports the existing registration and
changes nothing. A slug already assigned to different contents cannot be
reused. Package and profile names use lowercase letters, digits, and hyphens;
profile names are at most 32 characters.

To correct a package slug, a developer with write access runs:

```powershell
imdepend package rename 2026-2-fxi 2026-2-fix
```

The command asks for confirmation and then replaces the old slug. It does not
keep an alias: commands or scripts using the old spelling must be updated.
The ZIP stays unchanged, and profiles already installed from it remain usable
because they retain its SHA-256 identity. A profile may display its saved old
slug while the repository is offline.

## Install, compare, and roll back profiles

Install a registered package into a new profile or update an existing one:

```powershell
imdepend profile install good 2026-2-fix
imdepend profile install latest 2026-2-next
imdepend profile list
imdepend profile show latest
imdepend status
```

The tool verifies the archive, extracts and checks it in a staging directory,
then switches the profile to the new installation. A repeated install of the
same exact package is a no-op. An update retains one previous installation:

```powershell
imdepend profile rollback latest
```

Rollback swaps the current and previous installations, so running it again
returns to the version you just left. Stop CppDepend processes launched from
the affected profile before updating or rolling it back. Other profiles can
remain in use.

## Launch CppDepend

Open the established GUI for a profile:

```powershell
imdepend gui good
```

If a package includes `CppDependLight\VisualCppDepend.exe`, that executable is
the established GUI and the root `VisualCppDepend.exe` is the new GUI. Open the
new GUI with:

```powershell
imdepend gui good --variant new
```

For a package without the Light executable, the root executable is the only,
established GUI; requesting the new variant reports that it is unavailable.

Run console analysis and wait for it to finish:

```powershell
imdepend console good -- "C:\work\Example.cdproj" /FullAnalysis /ByVisualNDepend
```

Everything after `--` is passed to `CppDepend.Console.exe`. Its output is
shown while it runs, and `imdepend` returns the console process's exit code.
This lets scripts and Codex tell when the analysis has finished.

Use `--help` on any command for its arguments. Inspection and management
commands also support `--json` for scripts and Codex.
