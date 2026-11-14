# Reproducible ERPNextSwiss-only overlay

Build context: `source/` from a full, pushed Git archive and
`source-manifest.json` mapping each archived file path to its SHA-256.
Build arguments: inspected immutable production `BASE_IMAGE` and full
`ERP_REVISION`. Use a unique candidate tag. The other apps and their labels are
inherited unchanged; the ERP app is installed and the complete Python environment
must pass `pip check`. Source integrity runs in the build and restored/live sites.

Before activation, recheck that production still uses the same base image. Take a
fresh complete site backup, restore it with the versioned isolated platform runner
and execute the native VAT acceptance before its cleanup. Preserve platform
restore checks, connector checks, candidate cleanup and normal deployment gates.
Import historical filing evidence separately only after the schema rollout; hash
all financial tables before and after. No external submission is part of this flow.
