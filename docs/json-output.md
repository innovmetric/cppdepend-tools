# imdepend JSON output

Management commands accept `--json` before or after the command. They write
one JSON object to stdout on success:

```json
{"schema_version":1,"command":"package.show","data":{"slug":"2026-2-fix","sha256":"<full SHA-256>","registration_slug":"2026-2-fix","summary":"Fixed build","archive_filename":"2026-2-fix-a1b2c3d4e5f6.zip","archive_size":123456,"layout":"light-based","registered_at":"2026-10-20T12:00:00Z","source_filename":"CppDepend.zip","manifest_filename":"2026-2-fix-a1b2c3d4e5f6.toml"}}
```

`schema_version` identifies this output format. `command` names the operation;
`data` contains its result. An optional `vendor_version` appears in package
results only when it was supplied during registration. The SHA-256 is always
the full 64-character archive hash. A saved profile's `installed_slug` may
retain an older spelling after a package rename; the hash is its identity.

| Command | `data` fields |
| --- | --- |
| `setup` | `package_root`, `profile_root`, `package_root_available` |
| `status` | `package_root`, `package_root_available`, `profile_root`, `profiles` |
| `package list` | `packages`: array of package records |
| `package show` | One package record |
| `package register` | One package record plus `already_registered` |
| `package rename` | One package record plus `previous_slug` |
| `profile list` | `profiles`: array of profile records |
| `profile show` | One profile record |
| `profile install` | One profile record plus `unchanged` |
| `profile rollback` | One profile record |

A package record has `slug`, `sha256`, `registration_slug`, `summary`,
`archive_filename`, `archive_size`, `layout`, `registered_at`,
`source_filename`, and `manifest_filename`. `layout` is `legacy` or
`light-based`. `package list` reads manifest metadata and archive size;
`package show` and `profile install` also verify the archive SHA-256.

A profile record has `name`, `path`, `current`, `previous`, and
`recovery_required`. `current` and `previous` are either `null` or generation
records with `schema_version`, `sha256`, `installed_slug`, and `layout`.
`recovery_required` means an interrupted update or rollback needs recovery
before another change. `status` and profile inspection work while the package
share is unavailable.

On failure, stdout is empty and stderr contains one JSON object:

```json
{"schema_version":1,"error":{"code":"package_not_found","message":"package is not registered: missing"}}
```

Exit code `0` means success, `2` means invalid command arguments, and `1`
means an operational failure. Use `error.code` for decisions; `message` is for
people and may change. The `gui` and `console` launch commands do not support
`--json`. `console` streams CppDepend output and returns the vendor process's
exit code after a successful launch.
